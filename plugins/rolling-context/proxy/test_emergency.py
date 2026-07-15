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


class HardCeiling(unittest.TestCase):
    """饥饿逃生阀硬顶:trigger×1.2 与窗口×95% 取小(1.20.2)。"""

    def test_1m_window_uses_trigger_margin(self):
        # 1M 窗口:180k×1.2=216k < 950k → 硬顶 216k
        self.assertEqual(server._hard_ceiling(180_000, 1_000_000), 216_000)

    def test_200k_window_capped_below_wall(self):
        # 200k 窗口:216k 越窗,夹回 190k(95%),保证先于撞墙触发
        self.assertEqual(server._hard_ceiling(180_000, 200_000), 190_000)

    def test_ceiling_always_above_trigger(self):
        # 硬顶必须严格高于 trigger,否则闸门形同虚设
        for trig, win in ((160_000, 200_000), (180_000, 1_000_000), (40_000, 200_000)):
            self.assertGreater(server._hard_ceiling(trig, win), trig)


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


class ClaimCompression(unittest.TestCase):
    """store.claim_compression():一把锁内原子完成「去重检查 + 登记压缩意图」。去重口径由 in_flight_only
    参数(调用方按「本发是否已注入压缩」给)决定:
    - False(本发未注入,默认):已转正 original_hashes / 待转正 pending_hashes / 意图 intent_hashes
      任一覆盖同段即跳过 —— 防在途落地的压缩被下一发近乎重复地再压一遍。
    - True(本发已注入,find_match 用了最深条目却仍超 trigger,真需更深一层):只认在途(pending/
      intent),不认已转正 —— 否则会话被自身旧压缩挡住、深压缩永不建立,一路涨到 CC autocompact
      兜底(1.19.x 回归)。
    取代原全局 already_compressing,关掉两个并发请求重复触发同一段压缩的 TOCTOU 窗口。"""

    def test_empty_store_claims_and_registers_intent(self):
        s = server.CompressionStore()
        e = s.claim_compression(["a", "b", "c"])
        self.assertIsNotNone(e)
        self.assertEqual(e["intent_hashes"], ["a", "b", "c"])
        self.assertIs(s.compressions[-1], e)

    # --- 默认口径 in_flight_only=False(本发未注入):任何覆盖都挡下 ---
    def test_claim_blocked_by_promoted_chain(self):
        s = server.CompressionStore()
        e = s.add()
        e["original_hashes"] = ["bbb", "ccc"]
        e["prefix"] = [{"role": "user", "content": "S"}]
        self.assertIsNone(s.claim_compression(["aaa", "bbb", "ccc", "ddd"]))

    def test_claim_blocked_by_pending_chain(self):
        s = server.CompressionStore()
        e = s.add()
        e["pending_hashes"] = ["bbb", "ccc"]
        e["pending"] = [{"role": "user", "content": "S"}]
        self.assertIsNone(s.claim_compression(["aaa", "bbb", "ccc"]))

    def test_claim_blocked_by_inflight_intent(self):
        # 另一在途压缩刚登记 intent(还没出 pending):并发的第二个请求同段不应再被 claim
        s = server.CompressionStore()
        first = s.claim_compression(["aaa", "bbb", "ccc"])
        self.assertIsNotNone(first)
        second = s.claim_compression(["aaa", "bbb", "ccc", "ddd"])  # 尾部多一条的紧邻请求
        self.assertIsNone(second)
        self.assertEqual(len(s.compressions), 1, "intent 已登记,不应重复建第二条")

    def test_claim_blocked_when_newer_entry_also_covers(self):
        # 本发未注入时,库里另有更新条目 newer 也覆盖同段 → 冗余,claim 应挡下
        s = server.CompressionStore()
        injected = s.add()
        injected["original_hashes"] = ["aaa", "bbb"]
        injected["prefix"] = [{"role": "user", "content": "S0"}]
        newer = s.add()
        newer["original_hashes"] = ["ccc", "ddd"]
        newer["prefix"] = [{"role": "user", "content": "S1"}]
        self.assertIsNone(
            s.claim_compression(["aaa", "bbb", "ccc", "ddd", "eee"], exclude=injected)
        )

    # --- 深压缩口径 in_flight_only=True(本发已注入却仍超 trigger):只认在途 ---
    def test_claim_succeeds_when_only_excluded_entry_covers(self):
        # 仅「注入所用条目」覆盖时,排除它 → 仍需进一步压缩,claim 应放行
        s = server.CompressionStore()
        injected = s.add()
        injected["original_hashes"] = ["bbb", "ccc"]
        injected["prefix"] = [{"role": "user", "content": "S"}]
        e = s.claim_compression(["aaa", "bbb", "ccc", "ddd"], exclude=injected, in_flight_only=True)
        self.assertIsNotNone(e)
        self.assertEqual(e["intent_hashes"], ["aaa", "bbb", "ccc", "ddd"])

    def test_claim_in_flight_only_ignores_completed_sibling(self):
        # 复现 85df5cf0 回归:本发注入了 injected(已排除),库里另有【已转正】兄弟条目 sibling 也覆盖
        # 前缀。旧行为:sibling 令 claim 返回 None → 深压缩永不建立,会话一路涨到 CC autocompact 兜底。
        # 修复后 in_flight_only=True:已转正条目不参与去重 → claim 放行,建立更深压缩。
        s = server.CompressionStore()
        injected = s.add()
        injected["original_hashes"] = ["aaa", "bbb"]
        injected["prefix"] = [{"role": "user", "content": "S0"}]
        sibling = s.add()
        sibling["original_hashes"] = ["ccc", "ddd"]
        sibling["prefix"] = [{"role": "user", "content": "S1"}]
        claimed = s.claim_compression(
            ["aaa", "bbb", "ccc", "ddd", "eee"], exclude=injected, in_flight_only=True
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["intent_hashes"], ["aaa", "bbb", "ccc", "ddd", "eee"])

    def test_claim_in_flight_only_still_blocks_inflight_sibling(self):
        # 边界:in_flight_only=True 忽略已转正,但同段的【在途】压缩仍要挡下(避免连发重复触发)
        s = server.CompressionStore()
        done = s.add()
        done["original_hashes"] = ["aaa", "bbb"]
        done["prefix"] = [{"role": "user", "content": "S"}]
        inflight = s.add()
        inflight["intent_hashes"] = ["aaa", "bbb", "ccc"]
        self.assertIsNone(
            s.claim_compression(["aaa", "bbb", "ccc", "ddd"], in_flight_only=True)
        )


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

    def test_child_promotion_keeps_parent_until_first_hit(self):
        """1.21.4 延迟 reap:promote 后父子共存(子可能收进了仍会漂移的近端消息、秒失配——
        7-9 事故),父保留兜底;子首次真实命中(reap_parent)才回收父。"""
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
        # promote 后:父仍在(兜底),子已转正,父子共存
        self.assertEqual(len(s.compressions), 2)
        self.assertIn(parent, s.compressions)
        self.assertEqual(child["original_hashes"], ["c0", "c1"])
        self.assertIsNone(child["pending"])
        self.assertIs(child.get("parent"), parent)
        # 持久化两条都在(重启后父仍能兜底)
        s2 = server.CompressionStore()
        self.assertEqual(len(s2.compressions), 2)

        # 子首次真实命中 → 兑现 reap:父被回收,子成唯一活条目
        self.assertTrue(s.reap_parent(child))
        self.assertEqual(len(s.compressions), 1)
        self.assertIs(s.compressions[0], child)
        self.assertNotIn("parent", child)
        # 幂等:再调无事
        self.assertFalse(s.reap_parent(child))

    def test_reap_parent_tolerates_pruned_parent(self):
        """父已被 prune/并发 reap 时,reap_parent 仍安全(remove 按 identity 容错)。"""
        s = server.CompressionStore()
        parent = s.add()
        parent["original_hashes"] = ["p0"]
        parent["prefix"] = [{"role": "user", "content": "S1"}]
        child = s.add()
        child["original_hashes"] = ["c0"]
        child["prefix"] = [{"role": "user", "content": "S2"}]
        child["parent"] = parent
        s.remove(parent)  # 模拟父先被 prune
        self.assertTrue(s.reap_parent(child))  # pop 到了引用,remove 无害
        self.assertEqual(len(s.compressions), 1)

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


class DepthRegressionWarning(unittest.TestCase):
    """_warn_depth_regression:同会话注入深度倒退(更深条目失配)时告警——7-9 事故的
    第一现场信号,当时靠人肉对比两行 Injecting 才发现 8071→6019。仅观测,不改行为。"""

    def setUp(self):
        server._inject_depth.clear()

    def test_warns_on_regression(self):
        server._warn_depth_regression("sess-a", 8071)
        with self.assertLogs(server.log, level="WARNING") as cm:
            server._warn_depth_regression("sess-a", 6019)
        self.assertTrue(any("depth regressed" in m for m in cm.output))

    def test_no_warn_on_monotonic_growth(self):
        server._warn_depth_regression("sess-a", 6019)
        with self.assertNoLogs(server.log, level="WARNING"):
            server._warn_depth_regression("sess-a", 8071)
            server._warn_depth_regression("sess-a", 8071)  # 持平也不告警

    def test_sessions_are_independent(self):
        server._warn_depth_regression("sess-a", 8071)
        with self.assertNoLogs(server.log, level="WARNING"):
            server._warn_depth_regression("sess-b", 31)  # 另一会话的首采样,不是倒退

    def test_capped_dict_resets_quietly(self):
        for i in range(server._INJECT_DEPTH_MAX_SESSIONS + 1):
            server._warn_depth_regression(f"s{i}", 100)
        server._warn_depth_regression("overflow", 50)  # 触发清空后首采样,不炸不告警
        self.assertLessEqual(len(server._inject_depth), server._INJECT_DEPTH_MAX_SESSIONS + 1)


class DepthRegressionForensics(unittest.TestCase):
    """深度倒退告警的现场取证(1.21.6,与 _log_no_match 同款 `_diff_chain_break`):倒退时不再
    只报「深度变浅」的数字——那只够发现问题,查 7-15 事故时靠人肉翻两条 Injecting 日志的 body
    大小猜断点在哪。现在直接对「上次命中的那条 entry」在本次请求里重新定位断点,报 STORED/
    INCOMING 两端内容。entry/msg_hashes/messages 缺一即静默退回旧的纯数字告警(不崩、不误报),
    覆盖 DepthRegressionWarning 组既有测试仍用的旧 2-参数调用形态。"""

    def setUp(self):
        server._inject_depth.clear()

    def _mk_entry_and_drifted_request(self):
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        entry = {"original_hashes": server._hash_messages(msgs[10:16]),
                 "_debug_messages": msgs[10:16]}
        request = list(msgs)
        request[13] = {"role": "user", "content": "DRIFTED"}  # 链[3](msgs[13])处漂移
        return entry, request

    def test_reports_break_point_when_entry_and_context_given(self):
        entry, request = self._mk_entry_and_drifted_request()
        server._warn_depth_regression("sess-a", 16, entry=entry)
        with self.assertLogs(server.log, level="WARNING") as cm:
            server._warn_depth_regression("sess-a", 10, entry=None,
                                           msg_hashes=server._hash_messages(request),
                                           messages=request)
        out = "\n".join(cm.output)
        self.assertIn("depth regressed", out)
        self.assertIn("matches 3/6", out)     # 链前 3 条匹配,断在 chain[3]
        self.assertIn("request offset 10", out)
        self.assertIn("m13", out)             # STORED 端原文
        self.assertIn("DRIFTED", out)         # INCOMING 端原文

    def test_falls_back_to_plain_message_without_entry(self):
        server._warn_depth_regression("sess-b", 100)  # 旧调用形态:不传 entry
        with self.assertLogs(server.log, level="WARNING") as cm:
            server._warn_depth_regression("sess-b", 50)
        out = "\n".join(cm.output)
        self.assertIn("depth regressed", out)
        self.assertNotIn("STORED", out)

    def test_falls_back_when_chain_head_absent_from_current_request(self):
        entry = {"original_hashes": server._hash_messages(
                     [{"role": "user", "content": "elsewhere"}]),
                 "_debug_messages": [{"role": "user", "content": "elsewhere"}]}
        server._warn_depth_regression("sess-c", 100, entry=entry)
        request = [{"role": "user", "content": "unrelated"}]
        with self.assertLogs(server.log, level="WARNING") as cm:
            server._warn_depth_regression("sess-c", 50, entry=None,
                                           msg_hashes=server._hash_messages(request),
                                           messages=request)
        out = "\n".join(cm.output)
        self.assertIn("depth regressed", out)
        self.assertNotIn("STORED", out)


class RememberRealUserIdWiring(unittest.TestCase):
    """回归守卫(1.21.5 发布前修复):remember_real_user_id / _summary_user_id 是 compressor 的
    模块级函数(配套模块级 _last_real_user_id),不是 RollingCompressor 实例方法。此前 server.py
    误写成 `compressor.remember_real_user_id(...)`(compressor 在 server 里是实例),导致每个带
    metadata 的真实请求一进来就 AttributeError、连接 reset——而这条路径当时无任何测试覆盖,带病
    上线。以下四条钉死正确接线,杜绝同类实例前缀误用再溜过。"""

    def setUp(self):
        import compressor
        self._saved = compressor._last_real_user_id

    def tearDown(self):
        import compressor
        compressor._last_real_user_id = self._saved

    def test_server_imports_module_level_helper(self):
        # 崩溃点:server.py 必须把模块级函数 import 进来,按函数调用而非实例方法调。
        self.assertTrue(callable(getattr(server, "remember_real_user_id", None)))

    def test_instance_has_no_such_method(self):
        # bug 本质:实例/类上根本没有该属性,任何 `compressor.remember_real_user_id` 必崩。
        self.assertFalse(hasattr(server.RollingCompressor, "remember_real_user_id"))

    def test_source_has_no_instance_prefixed_call(self):
        # 源码守卫:防实例前缀误用复发(remember_real_user_id 与 _summary_user_id 都是模块级)。
        with open(os.path.join(os.path.dirname(server.__file__), "server.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("compressor.remember_real_user_id", src)
        self.assertNotIn("compressor._summary_user_id", src)

    def test_remember_then_summary_reuses_real_value(self):
        # 功能闭环:server 记住真实 user_id 后,回退摘要(client_meta 缺失)复用真值,不落兜底。
        import compressor
        compressor._last_real_user_id = None
        server.remember_real_user_id("user_realdev_account__session_zzz")
        self.assertEqual(compressor._last_real_user_id,
                         "user_realdev_account__session_zzz")
        self.assertEqual(compressor._summary_user_id(None),
                         "user_realdev_account__session_zzz")


class NoMatchSlidingDiagnostics(unittest.TestCase):
    """_log_no_match 滑窗部分匹配:此前按 0 偏移比对,对链在 raw 中部的增量子条目永远输出
    「diff at [0]」废话(7-9 排障实翻车)。现在要求报告最长部分匹配的断点及两端内容。"""

    def _mk_store(self):
        s = server.CompressionStore()
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        entry = s.add()
        # 链对应「请求中部」msgs[10:16],且在第 3 条(链[3]=msgs[13])处与请求漂移
        covered = msgs[10:16]
        entry["original_hashes"] = server._hash_messages(covered)
        entry["prefix"] = [{"role": "user", "content": "[ROLLING_CONTEXT_SUMMARY] x"}]
        entry["_debug_messages"] = covered
        request = list(msgs)
        request[13] = {"role": "user", "content": "DRIFTED"}  # 链[3] 位置内容漂移
        return s, request

    def test_reports_partial_match_break_position(self):
        s, request = self._mk_store()
        with self.assertLogs(server.log, level="DEBUG") as cm:
            best, end = s.find_match(server._hash_messages(request), request)
        self.assertIsNone(best)
        out = "\n".join(cm.output)
        self.assertIn("best partial: 3/6", out)          # 链前 3 条匹配,断在 chain[3]
        self.assertIn("request offset 10", out)          # 中部偏移被正确报告(旧实现只会说 [0])
        self.assertIn("m13", out)                        # STORED 端原文
        self.assertIn("DRIFTED", out)                    # INCOMING 端原文

    def test_no_shared_head_reports_cleanly(self):
        s = server.CompressionStore()
        entry = s.add()
        entry["original_hashes"] = server._hash_messages([{"role": "user", "content": "elsewhere"}])
        entry["prefix"] = [{"role": "user", "content": "[ROLLING_CONTEXT_SUMMARY] x"}]
        entry["_debug_messages"] = [{"role": "user", "content": "elsewhere"}]
        request = [{"role": "user", "content": "unrelated"}]
        with self.assertLogs(server.log, level="DEBUG") as cm:
            best, _ = s.find_match(server._hash_messages(request), request)
        self.assertIsNone(best)
        self.assertTrue(any("no candidate's chain head" in m for m in cm.output))


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
        server.compressor.compress = lambda m, h, real_token_count=None, client_meta=None: (compressed, 2)
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
        server.compressor.compress = lambda m, h, real_token_count=None, client_meta=None: (compressed, 2)
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
        server.compressor.compress = lambda m, h, real_token_count=None, client_meta=None: ([], 0)
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
        server.compressor.compress = lambda m, h, real_token_count=None, client_meta=None: (
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
    """_should_proactive_compress = 开关开 + 非 count +(未注入超 trigger | 已注入仍超窗)。
    body 粗估偏低估,故据此判超是保守的(只在请求确实很大时触发)。
    1.21.4 起返回 (should_sync, reason) 元组;injected 不再无条件放行——注入深度倒退后
    保留段仍可超窗(7-9 事故 1.8M),est 超窗就同步重压。"""

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
        should, reason = server._should_proactive_compress(644_001, {}, False, False)
        self.assertTrue(should)
        self.assertEqual(reason, "over-trigger")

    def test_no_fire_when_estimate_under_trigger(self):
        should, _ = server._should_proactive_compress(600_000, {}, False, False)  # //4=150000
        self.assertFalse(should)

    def test_no_fire_for_count_probe(self):
        should, _ = server._should_proactive_compress(10_000_000, {}, True, False)
        self.assertFalse(should)

    def test_injected_within_window_forwards(self):
        # 已注入且注入结果在窗口内(200k 窗,700000//4=175k < 200k)→ 放行,不再主动压
        should, reason = server._should_proactive_compress(700_000, {}, False, True)
        self.assertFalse(should)
        self.assertEqual(reason, "cache-hit")

    def test_injected_still_over_window_syncs(self):
        # 7-9 事故形态:注入深度倒退,est 仍超模型窗口(200k 窗,10M//4=2.5M)→ 同步重压不放行
        should, reason = server._should_proactive_compress(10_000_000, {}, False, True)
        self.assertTrue(should)
        self.assertEqual(reason, "injected-over-window")

    def test_no_fire_when_disabled(self):
        server.PROACTIVE_COMPRESS = False
        should, _ = server._should_proactive_compress(10_000_000, {}, False, False)
        self.assertFalse(should)

    def test_1m_window_raises_the_bar(self):
        server.TRIGGER_TOKENS = 320_000
        h = {"anthropic-beta": "context-1m-2025-08-07"}  # 真 1M → eff_trigger=320k(<900k 不夹)
        self.assertFalse(server._should_proactive_compress(1_000_000, h, False, False)[0])  # //4=250000
        self.assertTrue(server._should_proactive_compress(1_400_001, h, False, False)[0])   # //4=350000

    def test_image_excess_bytes_deducts_base64_over_cap(self):
        # 600KB 图片 base64 → 真实 ~600 tok(b64//1000),超额 = 600000 - 600*4 = 597600
        b64 = "A" * 600_000
        msgs = [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
        ]}]
        self.assertEqual(server._image_excess_bytes(msgs), 600_000 - 600 * 4)

    def test_image_excess_bytes_sees_nested_tool_result_images(self):
        b64 = "B" * 2_000_000  # 超大图,token 封顶 1600 → 超额 = 2000000 - 6400
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
            ]},
        ]}]
        self.assertEqual(server._image_excess_bytes(msgs), 2_000_000 - 6_400)

    def test_image_excess_bytes_zero_without_images(self):
        msgs = [{"role": "user", "content": "plain text"},
                {"role": "user", "content": [{"type": "text", "text": "block"}]}]
        self.assertEqual(server._image_excess_bytes(msgs), 0)


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
        server.compressor.compress = lambda m, h, real_token_count=None, client_meta=None: (
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


class DisguiseClient(unittest.TestCase):
    """客户端伪装:_apply_disguise 三态 + _maybe_capture_disguise 刷新条件。"""

    def setUp(self):
        self._orig_flag = server.DISGUISE_CLIENT
        self._orig_tmpl = server._disguise_template
        server._disguise_template = None

    def tearDown(self):
        server.DISGUISE_CLIENT = self._orig_flag
        server._disguise_template = self._orig_tmpl

    def test_apply_passthrough_when_disabled(self):
        server.DISGUISE_CLIENT = False
        server._disguise_template = {"user-agent": "claude-cli/1.0.83"}
        auth = {"authorization": "Bearer x", "user-agent": "py"}
        self.assertEqual(server._apply_disguise(auth), auth)

    def test_apply_passthrough_when_no_template(self):
        server.DISGUISE_CLIENT = True
        server._disguise_template = None
        auth = {"authorization": "Bearer x", "user-agent": "py"}
        self.assertEqual(server._apply_disguise(auth), auth)

    def test_apply_uses_template_but_keeps_auth(self):
        server.DISGUISE_CLIENT = True
        server._disguise_template = {
            "user-agent": "claude-cli/1.0.83",
            "x-app": "cli",
            "x-stainless-os": "MacOS",
            "anthropic-version": "2023-06-01",
        }
        auth = {
            "authorization": "Bearer SECRET",
            "x-api-key": "KEY",
            "user-agent": "Python-urllib/3.12",
            "anthropic-version": "2023-06-01",
        }
        out = server._apply_disguise(auth)
        self.assertEqual(out["user-agent"], "claude-cli/1.0.83")
        self.assertEqual(out["x-app"], "cli")
        self.assertEqual(out["x-stainless-os"], "MacOS")
        self.assertEqual(out["authorization"], "Bearer SECRET")
        self.assertEqual(out["x-api-key"], "KEY")

    def test_capture_refreshes_on_large_request(self):
        server.DISGUISE_CLIENT = True
        big = server.TARGET_TOKENS * 4 + 8
        headers = {
            "user-agent": "claude-cli/1.0.83",
            "authorization": "Bearer SECRET",
            "x-api-key": "KEY",
            "host": "127.0.0.1",
            "content-length": "999",
            "accept-encoding": "gzip",
            "x-app": "cli",
        }
        server._maybe_capture_disguise(headers, big, is_count=False)
        tmpl = server._disguise_template
        self.assertIsNotNone(tmpl)
        self.assertEqual(tmpl["user-agent"], "claude-cli/1.0.83")
        self.assertEqual(tmpl["x-app"], "cli")
        for k in ("authorization", "x-api-key", "host", "content-length", "accept-encoding"):
            self.assertNotIn(k, tmpl)

    def test_capture_ignores_small_request(self):
        server.DISGUISE_CLIENT = True
        server._maybe_capture_disguise({"user-agent": "small"}, 100, is_count=False)
        self.assertIsNone(server._disguise_template)

    def test_capture_ignores_count_tokens(self):
        server.DISGUISE_CLIENT = True
        big = server.TARGET_TOKENS * 4 + 8
        server._maybe_capture_disguise({"user-agent": "probe"}, big, is_count=True)
        self.assertIsNone(server._disguise_template)

    def test_capture_noop_when_disabled(self):
        server.DISGUISE_CLIENT = False
        big = server.TARGET_TOKENS * 4 + 8
        server._maybe_capture_disguise({"user-agent": "x"}, big, is_count=False)
        self.assertIsNone(server._disguise_template)

    def test_capture_then_apply_uses_large_request_ua(self):
        server.DISGUISE_CLIENT = True
        big = server.TARGET_TOKENS * 4 + 8
        server._maybe_capture_disguise(
            {"user-agent": "claude-cli/1.0.83", "authorization": "Bearer BIG"},
            big, is_count=False,
        )
        out = server._apply_disguise({"authorization": "Bearer CUR", "x-api-key": "K"})
        self.assertEqual(out["user-agent"], "claude-cli/1.0.83")
        self.assertEqual(out["authorization"], "Bearer CUR")

    def test_apply_overrides_session_id_with_current(self):
        # 模板带「上次大请求」的会话头,当次请求属另一会话 → 摘要头必须用当次会话(伪装形态仍来自模板)。
        server.DISGUISE_CLIENT = True
        server._disguise_template = {
            "user-agent": "claude-cli/1.0.83",
            "x-claude-code-session-id": "AAAAAAAA-old-session",
        }
        auth = {
            "authorization": "Bearer CUR",
            "x-claude-code-session-id": "BBBBBBBB-current-session",
        }
        out = server._apply_disguise(auth)
        self.assertEqual(out["user-agent"], "claude-cli/1.0.83")
        self.assertEqual(out["x-claude-code-session-id"], "BBBBBBBB-current-session")

    def test_apply_session_id_override_no_duplicate_header(self):
        # 模板键与当次键大小写不同,覆盖后只能剩一个会话头,否则上游可能取到旧会话值。
        server.DISGUISE_CLIENT = True
        server._disguise_template = {"X-Claude-Code-Session-Id": "AAAAAAAA-old"}
        out = server._apply_disguise({"x-claude-code-session-id": "BBBBBBBB-cur"})
        session_keys = [k for k in out if k.lower() == "x-claude-code-session-id"]
        self.assertEqual(len(session_keys), 1)
        self.assertEqual(out[session_keys[0]], "BBBBBBBB-cur")

    def test_apply_drops_stale_session_id_when_current_absent(self):
        # 模板带上次会话头,但当次请求【没带】session-id → 必须删掉,不能让摘要挂到上次会话名下。
        server.DISGUISE_CLIENT = True
        server._disguise_template = {
            "user-agent": "claude-cli/1.0.83",
            "x-claude-code-session-id": "AAAAAAAA-stale",
        }
        out = server._apply_disguise({"authorization": "Bearer CUR"})
        self.assertEqual(out["user-agent"], "claude-cli/1.0.83")  # 伪装形态仍保留
        self.assertNotIn("x-claude-code-session-id", {k.lower() for k in out})


class DecideCompression(unittest.TestCase):
    """decide_compression 决策表穷举——两处判定点的唯一事实来源,新增条件必须先过这张矩阵。"""

    # ---- stage="pre"(转发前,粗估口径) ----

    def test_pre_disabled(self):
        self.assertEqual(server.decide_compression("pre", est_tokens=999_999, eff_trigger=180_000,
                                                   proactive_enabled=False), ("forward", "disabled"))

    def test_pre_count_probe(self):
        self.assertEqual(server.decide_compression("pre", est_tokens=999_999, eff_trigger=180_000,
                                                   is_count=True), ("forward", "count-probe"))

    def test_pre_cache_hit(self):
        # 注入且未超窗(或未知窗口)→ 放行。window=0(未知)时保守放行,维持 1.21.3 前行为。
        self.assertEqual(server.decide_compression("pre", est_tokens=999_999, eff_trigger=180_000,
                                                   injected=True), ("forward", "cache-hit"))

    def test_pre_injected_within_window_forwards(self):
        self.assertEqual(server.decide_compression("pre", est_tokens=740_000, eff_trigger=180_000,
                                                   window=1_000_000, injected=True),
                         ("forward", "cache-hit"))

    def test_pre_injected_over_window_syncs(self):
        # 7-9 事故:注入深度倒退(命中更浅旧条目),保留段 est 1.8M > 1M 窗 → 原样转发必 400,
        # 必须当场同步重压。「注入 = known-good 尺寸」假设的矩阵化否定。
        self.assertEqual(server.decide_compression("pre", est_tokens=1_806_356, eff_trigger=180_000,
                                                   window=1_000_000, injected=True),
                         ("sync", "injected-over-window"))

    def test_pre_not_injected_ignores_window(self):
        # 未注入路径不受 window 参数影响:超 trigger 就 sync(原语义不变)
        self.assertEqual(server.decide_compression("pre", est_tokens=180_001, eff_trigger=180_000,
                                                   window=1_000_000), ("sync", "over-trigger"))

    def test_pre_over_trigger_syncs(self):
        self.assertEqual(server.decide_compression("pre", est_tokens=180_001, eff_trigger=180_000),
                         ("sync", "over-trigger"))

    def test_pre_under_or_at_trigger_forwards(self):
        self.assertEqual(server.decide_compression("pre", est_tokens=180_000, eff_trigger=180_000),
                         ("forward", "under-trigger"))

    # ---- stage="post"(响应后,上游真实 token 口径) ----

    def test_post_no_usage_skips(self):
        self.assertEqual(server.decide_compression("post", real_tokens=0, eff_trigger=180_000,
                                                   hard_ceiling=216_000, stop_reason="end_turn"),
                         ("skip", "no-usage"))

    def test_post_under_trigger_skips(self):
        self.assertEqual(server.decide_compression("post", real_tokens=180_000, eff_trigger=180_000,
                                                   hard_ceiling=216_000, stop_reason="end_turn"),
                         ("skip", "under-trigger"))

    def test_post_end_turn_compresses(self):
        self.assertEqual(server.decide_compression("post", real_tokens=180_001, eff_trigger=180_000,
                                                   hard_ceiling=216_000, stop_reason="end_turn"),
                         ("bg", "end-turn"))

    def test_post_tool_loop_defers_under_ceiling(self):
        # 问题②闸门:循环中超 trigger 但未超硬顶 → 推迟等干净切点
        self.assertEqual(server.decide_compression("post", real_tokens=190_000, eff_trigger=180_000,
                                                   hard_ceiling=216_000, stop_reason="tool_use"),
                         ("defer", "tool-loop"))

    def test_post_tool_loop_at_ceiling_still_defers(self):
        self.assertEqual(server.decide_compression("post", real_tokens=216_000, eff_trigger=180_000,
                                                   hard_ceiling=216_000, stop_reason="tool_use"),
                         ("defer", "tool-loop"))

    def test_post_starvation_over_ceiling_compresses(self):
        # 饥饿逃生阀:循环中超硬顶 → 强制建条目(fa861c68 480k 实案路径)
        self.assertEqual(server.decide_compression("post", real_tokens=480_737, eff_trigger=200_000,
                                                   hard_ceiling=240_000, stop_reason="tool_use"),
                         ("bg", "starvation"))

    def test_post_max_tokens_treated_as_loop(self):
        # stop_reason 非 end_turn 的其他值(max_tokens 等)与 tool_use 同款处理
        self.assertEqual(server.decide_compression("post", real_tokens=190_000, eff_trigger=180_000,
                                                   hard_ceiling=216_000, stop_reason="max_tokens"),
                         ("defer", "tool-loop"))

    def test_unknown_stage_raises(self):
        with self.assertRaises(ValueError):
            server.decide_compression("mid")


class HashResumeImmunity(unittest.TestCase):
    """哈希必须对 CC resume 时的消息改写免疫,否则恢复会话后深条目全部失配(480k 裸奔实案)。"""

    def test_thinking_block_stripped_from_hash(self):
        # 会话进行中:assistant 消息带 thinking+signature;resume 后 CC 剥掉 thinking 块
        live = {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "我先确认能否连上 lisa 主机", "signature": "CAISxQsKYgg"},
            {"type": "text", "text": "我先确认能否连上 lisa 主机,再找日志位置。"},
            {"type": "tool_use", "id": "toolu_019cu", "name": "Bash", "input": {"command": "ssh lisa true"}},
        ]}
        resumed = {"role": "assistant", "content": [b for b in live["content"] if b["type"] != "thinking"]}
        self.assertEqual(server._hash_message(live), server._hash_message(resumed))

    def test_redacted_thinking_stripped_from_hash(self):
        live = {"role": "assistant", "content": [
            {"type": "redacted_thinking", "data": "opaque-bytes"},
            {"type": "text", "text": "answer"},
        ]}
        resumed = {"role": "assistant", "content": [{"type": "text", "text": "answer"}]}
        self.assertEqual(server._hash_message(live), server._hash_message(resumed))

    def test_system_reminder_stripped_from_hash(self):
        # 回归:确认 _VOLATILE_TAGS_RE 真的在剥 reminder(reminder 会在请求间被 CC 挪动/移除)
        with_reminder = {"role": "user", "content": [
            {"type": "text", "text": "本地部署了吗\n<system-reminder>The user sent a new message.</system-reminder>"},
        ]}
        without = {"role": "user", "content": [{"type": "text", "text": "本地部署了吗\n"}]}
        self.assertEqual(server._hash_message(with_reminder), server._hash_message(without))

    def test_real_content_change_still_detected(self):
        # 免疫不能过度:正文/工具输入变了必须失配
        a = {"role": "assistant", "content": [{"type": "text", "text": "部署完成"}]}
        b = {"role": "assistant", "content": [{"type": "text", "text": "部署失败"}]}
        self.assertNotEqual(server._hash_message(a), server._hash_message(b))
        c = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}
        d = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "rm"}}]}
        self.assertNotEqual(server._hash_message(c), server._hash_message(d))


if __name__ == "__main__":
    unittest.main(verbosity=2)
