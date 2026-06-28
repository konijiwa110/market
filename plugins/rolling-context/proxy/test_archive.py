"""Unit tests for output-breakdown parsing + big/slow-request archiving (server.py).

Hermetic: redirects ROLLING_CONTEXT_STATE_DIR to a temp dir BEFORE importing server,
so importing the module (which builds a StatsCollector and a log handler under _CLAUDE_DIR)
never touches the real ~/.claude. The archive dir and size cap are monkeypatched per-test.

Covers:
  - _parse_output_blocks: streaming SSE split into thinking/text/tool_use char counts +
    ordered readable blocks; signature_delta counts as thinking but adds no readable text;
    malformed lines skipped
  - _parse_output_blocks_json: non-streaming response content array
  - _should_archive: output / time threshold boundaries; compression-kind + disabled skip
  - _write_archive: redacts secrets, gzip round-trips, records filename + breakdown
  - _prune_archive_dir: total-size cap removes oldest, keeps newest

Run:  python -m unittest test_archive      (from this proxy/ dir)
   or  python test_archive.py
"""
import os
import sys
import gzip
import json
import tempfile
import shutil
import unittest

# Redirect state dir before importing server (its import builds a StatsCollector + log handler
# under _CLAUDE_DIR). pytest imports every test module at collection time, alphabetically — this
# module sorts before test_emergency, so without care our `import server` would be the cached one
# its STATE_DIR isolation check then inspects. We therefore: (1) bind server to OUR temp dir for
# this module's tests, then (2) pop it from sys.modules and restore env, so sibling modules
# re-import server fresh under their own state-dir env (original import topology preserved).
_TMP_STATE = tempfile.mkdtemp(prefix="rc-archive-state-")
os.makedirs(os.path.join(_TMP_STATE, ".claude"), exist_ok=True)
_PRIOR_ENV = {k: os.environ.get(k) for k in ("ROLLING_CONTEXT_STATE_DIR", "HOME", "USERPROFILE")}
os.environ["ROLLING_CONTEXT_STATE_DIR"] = _TMP_STATE
os.environ["HOME"] = _TMP_STATE
os.environ["USERPROFILE"] = _TMP_STATE

import server  # noqa: E402

sys.modules.pop("server", None)  # let sibling test modules re-import fresh under their own env
for _k, _v in _PRIOR_ENV.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


def _sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


class OutputBreakdown(unittest.TestCase):
    def test_streaming_splits_thinking_text_tool(self):
        buf = _sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "hmmmm"}},          # 5 chars
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "hi"}},                     # 2 chars
            {"type": "content_block_start", "index": 2,
             "content_block": {"type": "tool_use", "name": "Edit"}},
            {"type": "content_block_delta", "index": 2,
             "delta": {"type": "input_json_delta", "partial_json": '{"a":1}'}},  # 7 chars
        )
        blocks, bd = server._parse_output_blocks(buf)
        self.assertEqual(bd["thinking"], 5)
        self.assertEqual(bd["text"], 2)
        self.assertEqual(bd["tool_use"], 7)
        self.assertEqual([b["type"] for b in blocks], ["thinking", "text", "tool_use"])
        self.assertEqual(blocks[2]["name"], "Edit")
        self.assertEqual(blocks[1]["text"], "hi")

    def test_signature_delta_is_thinking_with_no_readable_text(self):
        buf = _sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "abc"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "XXXXXXXX"}},
        )
        blocks, bd = server._parse_output_blocks(buf)
        self.assertEqual(bd["thinking"], 3)   # signature bytes not added to readable text
        self.assertEqual(bd["text"], 0)

    def test_non_streaming_json(self):
        data = {"content": [
            {"type": "thinking", "thinking": "abcd"},
            {"type": "text", "text": "xy"},
            {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
        ]}
        blocks, bd = server._parse_output_blocks_json(data)
        self.assertEqual(bd["thinking"], 4)
        self.assertEqual(bd["text"], 2)
        self.assertGreater(bd["tool_use"], 0)
        self.assertEqual(blocks[2]["name"], "Bash")

    def test_malformed_lines_skipped(self):
        buf = ("data: not-json\n\n"
               + _sse(
                   {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                   {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"}})
               + "event: ping\ndata: \n\n")
        _blocks, bd = server._parse_output_blocks(buf)
        self.assertEqual(bd["text"], 2)


class ShouldArchive(unittest.TestCase):
    def setUp(self):
        self._save = (server.ARCHIVE, server.ARCHIVE_MIN_OUT, server.ARCHIVE_MIN_MS)
        server.ARCHIVE = True
        server.ARCHIVE_MIN_OUT = 8000
        server.ARCHIVE_MIN_MS = 90000

    def tearDown(self):
        server.ARCHIVE, server.ARCHIVE_MIN_OUT, server.ARCHIVE_MIN_MS = self._save

    def test_triggers_on_output(self):
        self.assertTrue(server._should_archive(
            {"kind": "request", "output_tokens": 8000, "t_total_ms": 1000}))

    def test_triggers_on_time(self):
        self.assertTrue(server._should_archive(
            {"kind": "request", "output_tokens": 10, "t_total_ms": 90000}))

    def test_below_both_thresholds(self):
        self.assertFalse(server._should_archive(
            {"kind": "request", "output_tokens": 7999, "t_total_ms": 89999}))

    def test_skips_compression_kind(self):
        self.assertFalse(server._should_archive(
            {"kind": "compression", "output_tokens": 99999, "t_total_ms": 999999}))

    def test_respects_disabled(self):
        server.ARCHIVE = False
        self.assertFalse(server._should_archive(
            {"kind": "request", "output_tokens": 99999, "t_total_ms": 999999}))


class WriteAndPrune(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rc-archive-")
        self._save_dir = server._ARCHIVE_DIR
        self._save_cap = server.ARCHIVE_CAP_MB
        server._ARCHIVE_DIR = self.dir

    def tearDown(self):
        server._ARCHIVE_DIR = self._save_dir
        server.ARCHIVE_CAP_MB = self._save_cap
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_redacts_and_roundtrips(self):
        server.ARCHIVE_CAP_MB = 200
        record = {"ts": 1700000000.0, "session": "abc", "status": 200,
                  "model": "claude-opus-4-8", "output_tokens": 12000, "t_total_ms": 150000,
                  "out_thinking_chars": 400, "out_text_chars": 80, "out_tool_chars": 30}
        payload = {"model": "claude-opus-4-8",
                   "metadata": {"authorization": "Bearer secret"},
                   "system": "sys",
                   "messages": [{"role": "user", "content": "sk-ABCDEFGHIJKLMNOPQRSTUV"}]}
        blocks = [{"type": "text", "text": "hello"}]
        fname = server._write_archive(record, payload, blocks)
        self.assertTrue(fname)
        self.assertEqual(record["archive_file"], fname)
        with gzip.open(os.path.join(self.dir, fname), "rt", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["request"]["metadata"]["authorization"], "<redacted>")
        self.assertEqual(doc["request"]["messages"][0]["content"], "<redacted>")  # sk- string scrubbed
        self.assertEqual(doc["response"]["blocks"][0]["text"], "hello")
        self.assertEqual(doc["meta"]["output_breakdown_chars"]["thinking"], 400)
        self.assertEqual(doc["meta"]["usage"]["output_tokens"], 12000)

    def test_prune_caps_total_size_keeps_newest(self):
        server.ARCHIVE_CAP_MB = 100  # large during writes so per-write prune is a no-op
        names = []
        for i in range(5):
            rec = {"ts": 1700000000.0 + i, "session": f"s{i}", "status": 200,
                   "output_tokens": 9000, "t_total_ms": 100000}
            payload = {"blob": ("x" * 200000) + str(i)}  # distinct, sizeable
            fn = server._write_archive(rec, payload, [])
            names.append(fn)
            os.utime(os.path.join(self.dir, fn), (1700000000 + i, 1700000000 + i))  # deterministic mtime
        total = sum(os.path.getsize(os.path.join(self.dir, n)) for n in names)
        cap = total // 2
        server._prune_archive_dir(cap)
        remaining = set(os.listdir(self.dir))
        new_total = sum(os.path.getsize(os.path.join(self.dir, n)) for n in remaining)
        self.assertLessEqual(new_total, cap)
        self.assertIn(names[-1], remaining)       # newest survives
        self.assertNotIn(names[0], remaining)     # oldest pruned


if __name__ == "__main__":
    unittest.main()
