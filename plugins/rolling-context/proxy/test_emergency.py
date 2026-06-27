"""Unit tests for the emergency compress-and-retry helpers — stdlib only, hermetic.

Covers the pure helpers behind the "proxy never over-sends" path (server.py):

  - _looks_too_long        : only "prompt too long" 400s trigger the retry, not auth/format 400s
  - _parse_reported_tokens : pull the real prompt-token count out of the upstream error body
  - _compression_key_hashes: back out the summarized-away hash chain, identically to the
                             background path (so a synchronous emergency entry matches later)

Importing server.py runs only module-level config + logging (main() is __main__-guarded), so
we redirect HOME/USERPROFILE + state dir at a temp dir BEFORE import to avoid touching the real
~/.claude or the live :5588 gateway.

Run:  python -m unittest test_emergency      (from this proxy/ dir)
   or  python test_emergency.py
"""
import os
import tempfile
import unittest

# Isolate before importing server: its debug log uses expanduser("~"), config reads state dir.
_TMP = tempfile.mkdtemp(prefix="rc-emg-")
os.makedirs(os.path.join(_TMP, ".claude"), exist_ok=True)  # server opens ~/.claude/...debug.log
os.environ["USERPROFILE"] = _TMP
os.environ["HOME"] = _TMP
os.environ["ROLLING_CONTEXT_STATE_DIR"] = _TMP
os.environ.setdefault("ANTHROPIC_BASE_URL", "https://example.invalid")

import server  # noqa: E402


class LooksTooLong(unittest.TestCase):
    def test_real_anthropic_too_long_body(self):
        body = b'{"type":"error","error":{"type":"invalid_request_error",' \
               b'"message":"prompt is too long: 2100398 tokens > 1000000 maximum"}}'
        self.assertTrue(server._looks_too_long(body))

    def test_maximum_token_phrasing_without_too_long(self):
        self.assertTrue(server._looks_too_long(b"exceeds the maximum number of input tokens"))

    def test_auth_400_is_not_too_long(self):
        self.assertFalse(server._looks_too_long(b'{"error":{"message":"invalid x-api-key"}}'))

    def test_garbage_bytes_do_not_crash(self):
        self.assertFalse(server._looks_too_long(b"\xff\xfe\x00bad"))


class ParseReportedTokens(unittest.TestCase):
    def test_extracts_first_big_number_before_tokens(self):
        body = b"prompt is too long: 2100398 tokens > 1000000 maximum"
        self.assertEqual(server._parse_reported_tokens(body), 2100398)

    def test_handles_thousands_separators(self):
        body = b"prompt is too long: 2,100,398 tokens > 1,000,000 maximum"
        self.assertEqual(server._parse_reported_tokens(body), 2100398)

    def test_returns_none_when_absent(self):
        self.assertIsNone(server._parse_reported_tokens(b"some unrelated 400"))


class CompressionKeyHashes(unittest.TestCase):
    def _msgs(self, n):
        # alternate user/assistant; distinct content so hashes differ
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
            for i in range(n)
        ]

    def test_keys_the_summarized_prefix_only(self):
        src = self._msgs(10)
        # clean mode: prefix_len=2 ([summary, ack]) + 3 verbatim recents
        compressed = [{"role": "user", "content": "SUMMARY"},
                      {"role": "assistant", "content": "ok"}] + src[7:]
        key_hashes, summarized = server._compression_key_hashes(src, compressed, 2)
        self.assertEqual(summarized, src[:7])
        self.assertEqual(key_hashes, server._hash_messages(src[:7]))
        self.assertEqual(len(key_hashes), 7)

    def test_matches_background_path_for_same_inputs(self):
        # the two call sites (_do_background_compression, _emergency_compress) must agree
        src = self._msgs(8)
        compressed = [{"role": "user", "content": "SUMMARY"},
                      {"role": "assistant", "content": "ok"}] + src[5:]
        a = server._compression_key_hashes(src, compressed, 2)
        b = server._compression_key_hashes(src, compressed, 2)
        self.assertEqual(a, b)

    def test_skips_prior_summary_pair_at_head(self):
        # when the source already starts with a [summary, ack] pair, key skips those 2
        src = [{"role": "user", "content": f"{server.SUMMARY_MARKER} prior summary"},
               {"role": "assistant", "content": "ack"}] + self._msgs(6)
        compressed = [{"role": "user", "content": "SUMMARY2"},
                      {"role": "assistant", "content": "ok"}] + src[6:]
        key_hashes, summarized = server._compression_key_hashes(src, compressed, 2)
        # summarized = src[:6] but the leading summary pair is dropped from the key
        self.assertEqual(summarized, src[2:6])
        self.assertEqual(key_hashes, server._hash_messages(src[2:6]))


class StorePersistence(unittest.TestCase):
    """Persisted store survives a restart → warm on boot → no cold full-history send."""

    def setUp(self):
        # per-test store file so cases don't bleed into each other
        self._fd, self._path = tempfile.mkstemp(prefix="rc-store-", suffix=".json")
        os.close(self._fd)
        os.remove(self._path)  # start absent; _load must treat missing as empty
        self._orig = server.STORE_FILE
        server.STORE_FILE = self._path

    def tearDown(self):
        server.STORE_FILE = self._orig
        for p in (self._path, self._path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def _seed(self, store, hashes, prefix_text="SUMMARY", used=True, pre=123):
        e = store.add()
        e["original_hashes"] = list(hashes)
        e["prefix"] = [{"role": "user", "content": prefix_text}]
        e["used"] = used
        e["pre_tokens"] = pre
        return e

    def test_missing_file_loads_empty(self):
        s = server.CompressionStore()
        self.assertEqual(s.compressions, [])

    def test_roundtrip_then_match_after_reload(self):
        s1 = server.CompressionStore()
        self._seed(s1, ["aaa", "bbb"], pre=4096)
        s1.persist()
        # fresh instance = a "restarted proxy" reading the same file
        s2 = server.CompressionStore()
        self.assertEqual(len(s2.compressions), 1)
        match, end = s2.find_match(["x", "aaa", "bbb", "y"])
        self.assertIsNotNone(match, "reloaded entry must match its hash chain")
        self.assertEqual(end, 3)
        self.assertEqual(match["prefix"], [{"role": "user", "content": "SUMMARY"}])
        self.assertEqual(match["pre_tokens"], 4096)
        # runtime-only fields must be re-initialized, not loaded
        self.assertIsNone(match["pending"])
        self.assertIsNone(match["thread"])

    def test_incomplete_entries_are_not_persisted(self):
        s1 = server.CompressionStore()
        s1.add()  # empty: no prefix, no hashes
        self._seed(s1, ["only", "this"])  # complete
        s1.persist()
        s2 = server.CompressionStore()
        self.assertEqual(len(s2.compressions), 1, "only complete entries survive a reload")

    def test_prune_keeps_most_recent(self):
        s1 = server.CompressionStore()
        total = server.STORE_MAX_ENTRIES + 10
        for i in range(total):
            self._seed(s1, [f"h{i}"], prefix_text=f"S{i}")
        s1.persist()
        s2 = server.CompressionStore()
        self.assertEqual(len(s2.compressions), server.STORE_MAX_ENTRIES)
        # the LAST one in must survive (latest compression covers the most history)
        last = s2.find_match([f"h{total - 1}"])
        self.assertIsNotNone(last[0], "most recent entry must be retained after prune")
        # the FIRST one in must have been dropped
        first = s2.find_match(["h0"])
        self.assertIsNone(first[0], "oldest entry must be pruned")

    def test_corrupt_file_loads_empty(self):
        with open(self._path, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json")
        s = server.CompressionStore()  # must not raise
        self.assertEqual(s.compressions, [])


class PromoteReaping(unittest.TestCase):
    """promote_pending reaps the parent so each session keeps only 1 live entry."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(prefix="rc-reap-", suffix=".json")
        os.close(self._fd)
        self._orig = server.STORE_FILE
        server.STORE_FILE = self._path

    def tearDown(self):
        server.STORE_FILE = self._orig
        for p in (self._path, self._path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_child_promotion_reaps_parent(self):
        s = server.CompressionStore()
        parent = s.add()  # an already-active prior compression for this session
        parent["original_hashes"] = ["p0", "p1"]
        parent["prefix"] = [{"role": "user", "content": "S1"}]
        parent["used"] = True
        child = s.add()  # next compression, built on top of parent
        child["pending"] = [{"role": "user", "content": "S2"}]
        child["pending_hashes"] = ["c0", "c1"]
        child["parent"] = parent

        n = s.promote_pending()
        self.assertEqual(n, 1)
        # parent is gone, child is the sole live entry, now promoted
        self.assertEqual(len(s.compressions), 1)
        self.assertIs(s.compressions[0], child)
        self.assertEqual(child["original_hashes"], ["c0", "c1"])
        self.assertIsNone(child["pending"])
        # and the reaped state is what got persisted
        s2 = server.CompressionStore()
        self.assertEqual(len(s2.compressions), 1)
        self.assertEqual(s2.compressions[0]["original_hashes"], ["c0", "c1"])

    def test_first_compression_has_no_parent_to_reap(self):
        s = server.CompressionStore()
        first = s.add()  # cold session: no prior compression
        first["pending"] = [{"role": "user", "content": "S"}]
        first["pending_hashes"] = ["h0"]
        # parent stays None (default)
        n = s.promote_pending()
        self.assertEqual(n, 1)
        self.assertEqual(len(s.compressions), 1, "nothing to reap; entry just promotes")

    def test_nothing_pending_is_a_noop(self):
        s = server.CompressionStore()
        e = s.add()
        e["original_hashes"] = ["x"]
        e["prefix"] = [{"role": "user", "content": "S"}]
        self.assertEqual(s.promote_pending(), 0)
        self.assertEqual(len(s.compressions), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
