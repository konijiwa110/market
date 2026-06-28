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
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

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


class StoreCovers(unittest.TestCase):
    """store.covers():库里是否已有(已转正的 original_hashes 或刚就绪待转正的 pending_hashes)压缩,
    其哈希链出现在给定 msg_hashes 中。用于响应末尾去重:在途慢全量请求回来时,覆盖它的压缩若已入库则
    不再重复触发。"""

    def test_empty_store_covers_nothing(self):
        self.assertFalse(server.CompressionStore().covers(["a", "b", "c"]))

    def test_promoted_chain_is_covered(self):
        s = server.CompressionStore()
        e = s.add()
        e["original_hashes"] = ["bbb", "ccc"]
        e["prefix"] = [{"role": "user", "content": "S"}]
        self.assertTrue(s.covers(["aaa", "bbb", "ccc", "ddd"]))

    def test_pending_chain_is_covered_before_promotion(self):
        # 压缩刚算完、还没被下一发 promote:仍应判已覆盖,关掉「完成但未转正」那段重复触发窗口
        s = server.CompressionStore()
        e = s.add()
        e["pending_hashes"] = ["bbb", "ccc"]
        e["pending"] = [{"role": "user", "content": "S"}]
        self.assertTrue(s.covers(["aaa", "bbb", "ccc"]))

    def test_non_matching_chain_not_covered(self):
        s = server.CompressionStore()
        e = s.add()
        e["original_hashes"] = ["xxx", "yyy"]
        e["prefix"] = [{"role": "user", "content": "S"}]
        self.assertFalse(s.covers(["aaa", "bbb", "ccc"]))

    def test_entry_without_any_hashes_ignored(self):
        s = server.CompressionStore()
        s.add()  # 全新空条目:original_hashes=[] 且无 pending_hashes
        self.assertFalse(s.covers(["a", "b"]))


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


class BackgroundCompressionPublish(unittest.TestCase):
    """The background path must publish pending_hashes BEFORE pending, so promote_pending
    (which gates on pending) can never observe pending set while pending_hashes is still None."""

    def test_pending_and_hashes_are_both_published(self):
        src = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
               for i in range(6)]
        # clean-mode result: [summary, ack] + last 2 verbatim → prefix_len=2, summarized=src[:4]
        compressed = [{"role": "user", "content": "SUMMARY"},
                      {"role": "assistant", "content": "ok"}] + src[4:]
        orig = server.compressor.compress
        server.compressor.compress = lambda m, h, real_token_count=None: (compressed, 2)
        try:
            entry = {"pending": None, "pending_hashes": None}  # standalone; not in any store
            server._do_background_compression(entry, src, {}, real_token_count=100, sess="t")
            # invariant: if pending is visible, hashes must be too (and non-empty)
            self.assertIsNotNone(entry["pending"])
            self.assertIsNotNone(entry["pending_hashes"])
            self.assertEqual(len(entry["pending_hashes"]), 4)  # src[:4] summarized
            self.assertEqual(entry["pending"], compressed[:2])
        finally:
            server.compressor.compress = orig


class MemoryCap(unittest.TestCase):
    """In-memory _compressions is capped (not just the on-disk file); busy entries survive."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(prefix="rc-cap-", suffix=".json")
        os.close(self._fd)
        os.remove(self._path)
        self._orig_file = server.STORE_FILE
        self._orig_max = server.STORE_MAX_ENTRIES
        server.STORE_FILE = self._path
        server.STORE_MAX_ENTRIES = 3

    def tearDown(self):
        server.STORE_FILE = self._orig_file
        server.STORE_MAX_ENTRIES = self._orig_max
        for p in (self._path, self._path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_idle_entries_are_capped(self):
        s = server.CompressionStore()
        for _ in range(10):
            s.add()
        self.assertLessEqual(len(s.compressions), 3, "idle entries must be capped to STORE_MAX_ENTRIES")

    def test_busy_entries_are_never_pruned(self):
        class _Alive:
            def is_alive(self):
                return True

        s = server.CompressionStore()
        e1, e2, e3 = s.add(), s.add(), s.add()  # cap=3, no prune yet
        e1["thread"] = _Alive()
        e2["thread"] = _Alive()  # two in-progress before the next add forces a prune
        s.add()  # 4th entry → prune evicts an idle one, never a busy one
        # entries are structurally identical dicts → compare by identity, not ==
        ids = [id(e) for e in s.compressions]
        self.assertLessEqual(len(s.compressions), 3)
        self.assertIn(id(e1), ids, "in-progress entry must survive prune")
        self.assertIn(id(e2), ids, "in-progress entry must survive prune")
        self.assertNotIn(id(e3), ids, "the oldest idle entry is the one evicted")


class EmergencyReturn(unittest.TestCase):
    """_emergency_compress returns (compressed, entry) so the caller can reap the entry later."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(prefix="rc-emgret-", suffix=".json")
        os.close(self._fd)
        self._orig_file = server.STORE_FILE
        self._orig_store = server.store
        server.STORE_FILE = self._path
        server.store = server.CompressionStore()  # isolated; _emergency_compress uses the global

    def tearDown(self):
        server.STORE_FILE = self._orig_file
        server.store = self._orig_store
        for p in (self._path, self._path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_success_returns_registered_entry(self):
        src = [{"role": "user", "content": f"m{i}"} for i in range(6)]
        compressed = [{"role": "user", "content": "SUMMARY"},
                      {"role": "assistant", "content": "ok"}]
        orig = server.compressor.compress
        server.compressor.compress = lambda m, h, real_token_count=None: (compressed, 2)
        try:
            msgs, entry = server._emergency_compress(src, {}, 5000, 1000)
            self.assertEqual(msgs, compressed)
            self.assertIsNotNone(entry)
            self.assertEqual(entry["prefix"], compressed[:2])
            self.assertTrue(entry["used"])
            self.assertIn(entry, server.store.compressions)
        finally:
            server.compressor.compress = orig

    def test_nothing_to_compress_returns_none_none(self):
        orig = server.compressor.compress
        server.compressor.compress = lambda m, h, real_token_count=None: ([], 0)
        try:
            msgs, entry = server._emergency_compress([{"role": "user", "content": "x"}], {}, None, 100)
            self.assertIsNone(msgs)
            self.assertIsNone(entry)
        finally:
            server.compressor.compress = orig


# --- fake upstream for the end-to-end emergency-retry test ---------------------------------

class _FakeResp400:
    status = 400
    reason = "Bad Request"

    def read(self):
        return (b'{"type":"error","error":{"type":"invalid_request_error",'
                b'"message":"prompt is too long: 2100398 tokens > 1000000 maximum"}}')

    def getheaders(self):
        return [("content-type", "application/json")]

    def getheader(self, name, default=None):
        return "application/json" if name.lower() == "content-type" else (default or "")

    def close(self):
        pass


class _FakeResp200:
    status = 200
    reason = "OK"

    def __init__(self):
        self._chunks = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":50}}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n',
        ]
        self._i = 0

    def getheaders(self):
        return [("content-type", "text/event-stream")]

    def read1(self, n=-1):
        if self._i >= len(self._chunks):
            return b""
        c = self._chunks[self._i]
        self._i += 1
        return c

    def read(self):
        return b"".join(self._chunks)

    def close(self):
        pass


class _FakeUpstream:
    """Factory: 1st connection answers 400 'too long', 2nd (the retry) answers 200 SSE."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, parent):
        self.parent = parent

    def request(self, method, path, body=None, headers=None):
        self.parent.calls += 1
        self._n = self.parent.calls

    def getresponse(self):
        return _FakeResp400() if self._n == 1 else _FakeResp200()

    def close(self):
        pass


class EmergencyRetryEndToEnd(unittest.TestCase):
    """Full path: a real proxy POST that the upstream first rejects as too-long must come back
    200 to the client after one synchronous compress + retry — CC never sees the 400."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(prefix="rc-e2e-", suffix=".json")
        os.close(self._fd)
        self._saved = (server._upstream_conn, server.compressor.compress, server.store,
                       server.STORE_FILE, server.EMERGENCY_COMPRESS)
        self.fake = _FakeUpstream()
        server._upstream_conn = self.fake
        # stub the summarizer so emergency compression is offline + deterministic
        server.compressor.compress = lambda m, h, real_token_count=None: (
            [{"role": "user", "content": "[ROLLING_CONTEXT_SUMMARY] s"},
             {"role": "assistant", "content": "ok"}], 2)
        server.STORE_FILE = self._path
        server.store = server.CompressionStore()
        server.EMERGENCY_COMPRESS = True

        self.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), server.ProxyHandler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        (server._upstream_conn, server.compressor.compress, server.store,
         server.STORE_FILE, server.EMERGENCY_COMPRESS) = self._saved
        for p in (self._path, self._path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_too_long_is_compressed_and_retried_to_200(self):
        body = json.dumps({
            "model": "claude-test", "stream": True,
            "messages": [{"role": "user", "content": "x" * 64}],
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/messages",
            data=body, method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status = r.status
                out = r.read()
        except urllib.error.HTTPError as e:
            self.fail(f"client saw upstream error {e.code} — emergency retry did not shield it")
        self.assertEqual(status, 200, "client must receive 200 after the synchronous retry")
        self.assertIn(b"message_start", out, "the retried 200 SSE body must reach the client")
        self.assertEqual(self.fake.calls, 2, "exactly one reject + one retry to upstream")
        # the synchronous compression must have been registered for future matches
        self.assertEqual(len(server.store.compressions), 1)


class StatsPathIsolation(unittest.TestCase):
    """统计落盘路径必须走 _CLAUDE_DIR(尊重 ROLLING_CONTEXT_STATE_DIR),与 pid/version/store 一致;
    否则隔离实例(本测试、DEV 备用实例)会污染真实 ~/.claude/rolling-context-stats.jsonl。"""

    def test_stats_path_under_isolated_state_dir(self):
        self.assertEqual(
            os.path.normcase(os.path.abspath(server.stats._path)),
            os.path.normcase(os.path.abspath(os.path.join(_TMP, "rolling-context-stats.jsonl"))),
            "stats must write under ROLLING_CONTEXT_STATE_DIR, not the real ~/.claude",
        )


class RequestWindow(unittest.TestCase):
    """_request_window 判出本请求真实窗口:anthropic-beta 含 context-1m → 1M,否则 200k;
    config 的 context_window 显式覆盖优先(供第三方上游谎报 1M 时钉死)。"""

    def setUp(self):
        self._orig = server.CONTEXT_WINDOW_OVERRIDE
        server.CONTEXT_WINDOW_OVERRIDE = 0  # 默认走头判定

    def tearDown(self):
        server.CONTEXT_WINDOW_OVERRIDE = self._orig

    def test_beta_header_context_1m_is_1m(self):
        h = {"anthropic-beta": "context-1m-2025-08-07"}
        self.assertEqual(server._request_window(h), server.WINDOW_1M)

    def test_beta_among_other_betas(self):
        h = {"anthropic-beta": "fine-grained-tool-streaming-2025-05-14,context-1m-2025-08-07"}
        self.assertEqual(server._request_window(h), server.WINDOW_1M)

    def test_no_beta_header_is_200k(self):
        self.assertEqual(server._request_window({"content-type": "application/json"}), server.WINDOW_DEFAULT)

    def test_beta_header_without_1m_is_200k(self):
        h = {"anthropic-beta": "fine-grained-tool-streaming-2025-05-14"}
        self.assertEqual(server._request_window(h), server.WINDOW_DEFAULT)

    def test_header_name_is_case_insensitive(self):
        h = {"Anthropic-Beta": "Context-1M-2025-08-07"}
        self.assertEqual(server._request_window(h), server.WINDOW_1M)

    def test_config_override_pins_window_over_header(self):
        server.CONTEXT_WINDOW_OVERRIDE = 200_000
        h = {"anthropic-beta": "context-1m-2025-08-07"}  # 上游谎报 1M
        self.assertEqual(server._request_window(h), 200_000, "config 钉死必须压过头判定")


class EffectiveTrigger(unittest.TestCase):
    """_effective_trigger = min(配置 trigger, 真实窗口×0.9):配超时夹紧、正常配置不受影响。"""

    def setUp(self):
        self._orig_trig = server.TRIGGER_TOKENS
        self._orig_ovr = server.CONTEXT_WINDOW_OVERRIDE
        server.CONTEXT_WINDOW_OVERRIDE = 0

    def tearDown(self):
        server.TRIGGER_TOKENS = self._orig_trig
        server.CONTEXT_WINDOW_OVERRIDE = self._orig_ovr

    def test_normal_trigger_under_200k_window_not_capped(self):
        server.TRIGGER_TOKENS = 160_000
        self.assertEqual(server._effective_trigger({}), 160_000)  # 200k 窗口,180k 夹线,160k<180k 不夹

    def test_high_trigger_capped_to_90pct_of_200k(self):
        server.TRIGGER_TOKENS = 320_000  # 为 1M 配高,但请求无 1m 头 → 实际 200k
        self.assertEqual(server._effective_trigger({}), 180_000)

    def test_high_trigger_under_1m_window_not_capped(self):
        server.TRIGGER_TOKENS = 320_000
        h = {"anthropic-beta": "context-1m-2025-08-07"}  # 真 1M,900k 夹线,320k<900k 不夹
        self.assertEqual(server._effective_trigger(h), 320_000)

    def test_override_pins_window_for_trigger(self):
        server.TRIGGER_TOKENS = 320_000
        server.CONTEXT_WINDOW_OVERRIDE = 200_000
        h = {"anthropic-beta": "context-1m-2025-08-07"}  # 头说 1M 但被 config 钉死 200k
        self.assertEqual(server._effective_trigger(h), 180_000)


class ProactiveGate(unittest.TestCase):
    """_should_proactive_compress = 开关开 + 非 count + 未注入 + body//4 超有效 trigger。
    body 粗估偏低估,故据此判超是保守的(只在请求确实很大时触发)。"""

    def setUp(self):
        self._saved = (server.PROACTIVE_COMPRESS, server.TRIGGER_TOKENS, server.CONTEXT_WINDOW_OVERRIDE)
        server.PROACTIVE_COMPRESS = True
        server.TRIGGER_TOKENS = 160_000
        server.CONTEXT_WINDOW_OVERRIDE = 0  # 走头判定

    def tearDown(self):
        server.PROACTIVE_COMPRESS, server.TRIGGER_TOKENS, server.CONTEXT_WINDOW_OVERRIDE = self._saved

    def test_estimate_is_body_bytes_over_four(self):
        self.assertEqual(server._estimate_body_tokens(800_000), 200_000)

    def test_fires_when_estimate_exceeds_trigger(self):
        # 无 1m 头 → 200k 窗口 → eff_trigger=160k;644001//4 = 161000 > 160000
        self.assertTrue(server._should_proactive_compress(644_001, {}, False, False))

    def test_no_fire_when_estimate_under_trigger(self):
        self.assertFalse(server._should_proactive_compress(600_000, {}, False, False))  # //4=150000

    def test_no_fire_for_count_probe(self):
        self.assertFalse(server._should_proactive_compress(10_000_000, {}, True, False))

    def test_no_fire_when_already_injected(self):
        # 已命中缓存注入(小请求)→ 不再主动压
        self.assertFalse(server._should_proactive_compress(10_000_000, {}, False, True))

    def test_no_fire_when_disabled(self):
        server.PROACTIVE_COMPRESS = False
        self.assertFalse(server._should_proactive_compress(10_000_000, {}, False, False))

    def test_1m_window_raises_the_bar(self):
        server.TRIGGER_TOKENS = 320_000
        h = {"anthropic-beta": "context-1m-2025-08-07"}  # 真 1M → eff_trigger=320k(<900k 不夹)
        self.assertFalse(server._should_proactive_compress(1_000_000, h, False, False))  # //4=250000
        self.assertTrue(server._should_proactive_compress(1_400_001, h, False, False))   # //4=350000


# --- fake upstream for the proactive end-to-end test ---------------------------------------

class _CapturingConn:
    def __init__(self, parent):
        self.parent = parent

    def request(self, method, path, body=None, headers=None):
        self.parent.calls += 1
        self.parent.last_body = body

    def getresponse(self):
        return _FakeResp200()

    def close(self):
        pass


class _CapturingUpstream:
    """工厂:每发都答 200 SSE(无 400),并记下转发给上游的请求体,供断言「上游只收到压后小体」。"""

    def __init__(self):
        self.calls = 0
        self.last_body = None

    def __call__(self):
        return _CapturingConn(self)


class ProactiveCompressEndToEnd(unittest.TestCase):
    """转发前主动压缩:一发超 trigger 的 cache-miss 大请求,必须在打上游【之前】就被压小——上游只收到
    一发(无 400 兜底)、且收到的是压后小体;压缩条目当场登记进 store 供后续命中。"""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(prefix="rc-prewarm-", suffix=".json")
        os.close(self._fd)
        self._saved = (server._upstream_conn, server.compressor.compress, server.store,
                       server.STORE_FILE, server.PROACTIVE_COMPRESS, server.TRIGGER_TOKENS,
                       server.CONTEXT_WINDOW_OVERRIDE)
        self.fake = _CapturingUpstream()
        server._upstream_conn = self.fake
        server.compressor.compress = lambda m, h, real_token_count=None: (
            [{"role": "user", "content": "[ROLLING_CONTEXT_SUMMARY] s"},
             {"role": "assistant", "content": "ok"}], 2)
        server.STORE_FILE = self._path
        server.store = server.CompressionStore()
        server.PROACTIVE_COMPRESS = True
        server.TRIGGER_TOKENS = 100          # 低门槛:让一发中等大小的体即超 trigger
        server.CONTEXT_WINDOW_OVERRIDE = 0

        self.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), server.ProxyHandler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        (server._upstream_conn, server.compressor.compress, server.store,
         server.STORE_FILE, server.PROACTIVE_COMPRESS, server.TRIGGER_TOKENS,
         server.CONTEXT_WINDOW_OVERRIDE) = self._saved
        for p in (self._path, self._path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_big_first_request_is_compressed_before_forward(self):
        giant = "x" * 4000  # body ~4KB → //4 ~1k tokens > trigger 100
        body = json.dumps({
            "model": "claude-test", "stream": True,
            "messages": [{"role": "user", "content": giant}],
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/messages",
            data=body, method="POST",
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            status = r.status
            out = r.read()
        self.assertEqual(status, 200)
        self.assertIn(b"message_start", out)
        self.assertEqual(self.fake.calls, 1, "上游应只收到一发(压后直发,无 400 兜底往返)")
        # 上游收到的是压后小体:含摘要标记、不含原始巨串
        self.assertIsNotNone(self.fake.last_body)
        self.assertIn(b"ROLLING_CONTEXT_SUMMARY", self.fake.last_body)
        self.assertNotIn(giant.encode(), self.fake.last_body, "原始全量消息不得直发上游")
        self.assertLess(len(self.fake.last_body), len(body), "转发体必须小于原始请求体")
        # 主动压缩条目已登记,供后续请求命中
        self.assertEqual(len(server.store.compressions), 1)


# --- fake upstream that lands a covering compression mid-flight (for the dedup test) ----------

class _MidFlightSeedConn:
    def __init__(self, parent):
        self.parent = parent

    def request(self, method, path, body=None, headers=None):
        self.parent.calls += 1

    def getresponse(self):
        # 此刻请求已转发、find_match 早已在请求开头跑过(空库 → 没命中)。模拟「覆盖本发的压缩在在途
        # 期间落地」:往库里塞一条 original_hashes == 本发消息哈希链的压缩,触发响应末尾的 covers 去重。
        self.parent.seed()
        return _FakeResp200()

    def close(self):
        pass


class _MidFlightSeedUpstream:
    def __init__(self, seed):
        self.calls = 0
        self.seed = seed

    def __call__(self):
        return _MidFlightSeedConn(self)


class RedundantCompressionSkip(unittest.TestCase):
    """在途慢全量请求回来时(发出时无命中 → 原样发,期间覆盖它的压缩落地),响应末尾不得再触发一条
    近乎重复的后台压缩 —— 复现并守住截图里那两条几乎一样的压缩。"""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(prefix="rc-dedup-", suffix=".json")
        os.close(self._fd)
        self._saved = (server._upstream_conn, server.store, server.STORE_FILE,
                       server.PROACTIVE_COMPRESS, server.EMERGENCY_COMPRESS,
                       server.TRIGGER_TOKENS, server.CONTEXT_WINDOW_OVERRIDE,
                       server._do_background_compression)
        server.store = server.CompressionStore()
        server.STORE_FILE = self._path
        server.PROACTIVE_COMPRESS = False   # 隔离去重逻辑:不让转发前主动压缩抢先注入
        server.EMERGENCY_COMPRESS = False
        server.TRIGGER_TOKENS = 10          # 让 fake 200 自报的 input=50 必定超 trigger
        server.CONTEXT_WINDOW_OVERRIDE = 0
        # bg 压缩替身:被调用即记一次、且不动 store,使「是否触发」可确定性断言
        self._bg_calls = []
        server._do_background_compression = lambda *a, **k: self._bg_calls.append(1)

        self._messages = [{"role": "user", "content": "m0"}, {"role": "user", "content": "m1"}]
        self._hashes = server._hash_messages(self._messages)

        def _seed():
            e = server.store.add()
            e["original_hashes"] = list(self._hashes)
            e["prefix"] = [{"role": "user", "content": "[ROLLING_CONTEXT_SUMMARY] s"}]
            e["used"] = True

        self.fake = _MidFlightSeedUpstream(_seed)
        server._upstream_conn = self.fake

        self.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), server.ProxyHandler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        (server._upstream_conn, server.store, server.STORE_FILE,
         server.PROACTIVE_COMPRESS, server.EMERGENCY_COMPRESS,
         server.TRIGGER_TOKENS, server.CONTEXT_WINDOW_OVERRIDE,
         server._do_background_compression) = self._saved
        for p in (self._path, self._path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_no_duplicate_compression_when_one_already_landed(self):
        body = json.dumps({"model": "claude-test", "stream": True,
                           "messages": self._messages}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/messages",
            data=body, method="POST", headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
            r.read()
        # 覆盖压缩已在途落地 → 响应末尾必须跳过,不再触发后台压缩
        self.assertEqual(self._bg_calls, [], "已有覆盖压缩时不得重复触发后台压缩")
        # 库里仍只有那条「落地的」压缩,没有新增重复条目
        self.assertEqual(len(server.store.compressions), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
