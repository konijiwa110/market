"""Unit test for the compressor empty-content guard — stdlib only, hermetic.

A 200 response whose `content` is empty (rare stop_reason / refusal) used to hit
`data["content"][0]["text"]` and raise a bare IndexError; the self-heal layer only catches
RuntimeError, so the whole compression failed uncaught. The guard converts it to a controlled
RuntimeError (and the finally still records one stat). This pins that behavior.

compressor.py touches no files/network on import, so no isolation is needed; we stub _conn().

Run:  python -m unittest test_compressor      (from this proxy/ dir)
   or  python test_compressor.py
"""
import json
import unittest

import compressor


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getheader(self, _name):
        return ""  # no Content-Encoding → no decompression branch


class _FakeConn:
    def __init__(self, resp):
        self._resp = resp

    def request(self, *a, **k):
        pass

    def getresponse(self):
        return self._resp

    def close(self):
        pass


class EmptyContentGuard(unittest.TestCase):
    def _compressor_returning(self, status, payload):
        c = compressor.RollingCompressor()
        resp = _FakeResp(status, json.dumps(payload).encode())
        c._conn = lambda: _FakeConn(resp)  # never touch the network
        return c

    def test_empty_content_raises_runtimeerror_not_indexerror(self):
        c = self._compressor_returning(
            200, {"content": [], "stop_reason": "refusal", "usage": {}})
        with self.assertRaises(RuntimeError):
            c._summarize_chunk("hi", "", {})

    def test_missing_content_key_raises_runtimeerror(self):
        c = self._compressor_returning(
            200, {"stop_reason": "end_turn", "usage": {}})
        with self.assertRaises(RuntimeError):
            c._summarize_chunk("hi", "", {})

    def test_normal_content_is_still_returned(self):
        c = self._compressor_returning(
            200, {"content": [{"type": "text", "text": "SUMMARY"}], "usage": {}})
        self.assertEqual(c._summarize_chunk("hi", "", {}), "SUMMARY")


class StripUnsupported1mBeta(unittest.TestCase):
    """判定挂在模型上:Haiku 不支持 1M → 剥掉 context-1m(保留其余 beta);支持 1M 的模型原样保留。"""

    def test_haiku_removes_context_1m_only(self):
        h = {"anthropic-beta": "fine-grained-tool-streaming-2025-05-14,context-1m-2025-08-07"}
        compressor._strip_unsupported_1m_beta(h, "claude-haiku-4-5-20251001")
        self.assertEqual(h["anthropic-beta"], "fine-grained-tool-streaming-2025-05-14")

    def test_haiku_drops_header_when_only_context_1m(self):
        h = {"anthropic-beta": "context-1m-2025-08-07"}
        compressor._strip_unsupported_1m_beta(h, "claude-haiku-latest")
        self.assertNotIn("anthropic-beta", h)

    def test_1m_capable_model_keeps_beta(self):
        h = {"anthropic-beta": "context-1m-2025-08-07"}
        compressor._strip_unsupported_1m_beta(h, "claude-opus-4-8")
        self.assertEqual(h["anthropic-beta"], "context-1m-2025-08-07", "支持 1M 的模型必须原样保留")

    def test_header_name_case_insensitive(self):
        h = {"Anthropic-Beta": "Context-1M-2025-08-07,foo-bar"}
        compressor._strip_unsupported_1m_beta(h, "claude-haiku-latest")
        self.assertEqual(h["Anthropic-Beta"], "foo-bar")

    def test_no_beta_header_is_noop(self):
        h = {"content-type": "application/json"}
        compressor._strip_unsupported_1m_beta(h, "claude-haiku-latest")
        self.assertEqual(h, {"content-type": "application/json"})

    def test_summarize_chunk_strips_beta_from_outgoing_request(self):
        captured = {}

        class _CapConn:
            def request(self, method, path, body=None, headers=None):
                captured.update(headers or {})
            def getresponse(self):
                return _FakeResp(200, json.dumps(
                    {"content": [{"type": "text", "text": "S"}], "usage": {}}).encode())
            def close(self):
                pass

        c = compressor.RollingCompressor()
        c._conn = lambda: _CapConn()
        c._summarize_chunk("hi", "", {"anthropic-beta": "context-1m-2025-08-07"})
        self.assertNotIn("anthropic-beta", {k.lower(): v for k, v in captured.items()})


class StripBodyCoupledBetas(unittest.TestCase):
    """摘要请求一律剥「门票型」beta(功能靠 body 字段驱动、头只是开门):摘要体永不带对应字段,
    带票曾诱发上游按 clear_thinking_20251015 策略校验 → 400「requires thinking to be enabled or
    adaptive」,压缩间歇性全灭(2026-07-08 实案)。compact/structured-outputs/effort 同族一并剥。"""

    def test_removes_context_management_only(self):
        # 事故形状:CC(fable, thinking adaptive)主请求的真实 beta 串形态
        h = {"anthropic-beta": "claude-code-20250219,oauth-2025-04-20,"
                               "context-management-2025-06-27,interleaved-thinking-2025-05-14"}
        compressor._strip_body_coupled_betas(h)
        self.assertEqual(h["anthropic-beta"],
                         "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14")

    def test_removes_whole_family(self):
        h = {"anthropic-beta": "claude-code-20250219,compact-2026-01-12,"
                               "structured-outputs-2025-12-15,effort-2025-11-24"}
        compressor._strip_body_coupled_betas(h)
        self.assertEqual(h["anthropic-beta"], "claude-code-20250219")

    def test_interleaved_thinking_is_kept(self):
        # 观察名单:实测无害且是 CC 头形态高频常客(拟真),明确不剥
        h = {"anthropic-beta": "interleaved-thinking-2025-05-14"}
        compressor._strip_body_coupled_betas(h)
        self.assertEqual(h["anthropic-beta"], "interleaved-thinking-2025-05-14")

    def test_drops_header_when_all_coupled(self):
        h = {"anthropic-beta": "context-management-2025-06-27,compact-2026-01-12"}
        compressor._strip_body_coupled_betas(h)
        self.assertNotIn("anthropic-beta", h)

    def test_header_name_and_token_case_insensitive(self):
        h = {"Anthropic-Beta": "Context-Management-2025-06-27,foo-bar"}
        compressor._strip_body_coupled_betas(h)
        self.assertEqual(h["Anthropic-Beta"], "foo-bar")

    def test_summarize_chunk_strips_it_from_outgoing_request(self):
        captured = {}

        class _CapConn:
            def request(self, method, path, body=None, headers=None):
                captured.update(headers or {})
            def getresponse(self):
                return _FakeResp(200, json.dumps(
                    {"content": [{"type": "text", "text": "S"}], "usage": {}}).encode())
            def close(self):
                pass

        c = compressor.RollingCompressor()
        c._conn = lambda: _CapConn()
        c._summarize_chunk("hi", "", {"anthropic-beta": "context-management-2025-06-27,foo-beta"})
        beta = {k.lower(): v for k, v in captured.items()}.get("anthropic-beta", "")
        self.assertEqual(beta, "foo-beta")

    def test_400_error_reports_sent_beta_header(self):
        """失败诊断:400 时 RuntimeError 与 err_snippet 必须带上实际发出的 anthropic-beta,
        beta 头诱发的 400 家族(1M / context-management)不再需要反推 CC 二进制定位。"""
        err_body = json.dumps({"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "`clear_thinking_20251015` strategy requires `thinking` to be enabled or adaptive"}})

        class _Conn400:
            def request(self, method, path, body=None, headers=None):
                pass
            def getresponse(self):
                return _FakeResp(400, err_body.encode())
            def close(self):
                pass

        stats = []
        c = compressor.RollingCompressor()
        c._conn = lambda: _Conn400()
        c.stats_sink = stats.append
        with self.assertRaises(RuntimeError) as ctx:
            c._summarize_chunk("hi", "", {"anthropic-beta": "foo-beta,bar-beta"})
        self.assertIn("sent anthropic-beta: foo-beta,bar-beta", str(ctx.exception))
        self.assertIn("sent anthropic-beta: foo-beta,bar-beta", stats[0]["err_snippet"])


class ChunkTailMerge(unittest.TestCase):
    """_chunk_by_chars 尾块合并:尾块 < 预算 15% 时并入前一块,省一次串行摘要调用(1.20.2)。
    单条消息文本 = "**user**: " + content(10 字符前缀),content 长度按目标尺寸减 10 构造。"""

    @staticmethod
    def _msg(text_len):
        return {"role": "user", "content": "x" * (text_len - 10)}

    def setUp(self):
        self.c = compressor.RollingCompressor()

    def test_tiny_tail_merged_into_previous(self):
        # 900/950/100,预算 1000 → 贪心切 3 块,尾块 100 < 150(15%)→ 并入前块
        msgs = [self._msg(900), self._msg(950), self._msg(100)]
        chunks = self.c._chunk_by_chars(msgs, 1000)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[1]), 2)  # 950 + 100 两条同块

    def test_normal_tail_not_merged(self):
        # 尾块 200 ≥ 150 → 保持 3 块不动
        msgs = [self._msg(900), self._msg(950), self._msg(200)]
        chunks = self.c._chunk_by_chars(msgs, 1000)
        self.assertEqual(len(chunks), 3)

    def test_single_chunk_untouched(self):
        msgs = [self._msg(100), self._msg(100)]
        chunks = self.c._chunk_by_chars(msgs, 1000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 2)


class SettledUserTurnBoundary(unittest.TestCase):
    """_is_settled_user_turn 与三处切点判定(_find_keep_index / _select_cut 前向扫描 / _select_cut
    后向兜底回退)统一改用它:CC 对带附件 tool_result 的历史表示不稳定,80 秒内同一段内容的
    消息数可能 1 条变 2 条(tool_result 文本 + 紧随其后的纯图片续消息)。旧逻辑只判「role=user
    且无 tool_result」,会把这条图片续消息误判成干净边界、把切点切在漂移续片段中间——该消息
    此后一旦改变分片形态,已固化的压缩哈希链末位就会失配,深度倒退回更浅的旧压缩
    (7-15 事故:0-9831 退到 0-5621,同会话当天 ≥5 次)。"""

    def setUp(self):
        self.c = compressor.RollingCompressor()

    @staticmethod
    def _tool_result_msg(n=40):
        return {"role": "user", "content": [{"type": "tool_result", "content": "d" * n}]}

    @staticmethod
    def _image_only_msg():
        # 短 base64(<1000 字符)→ _image_chars 固定按 4 字符当量计,便于精确控制预算
        return {"role": "user", "content": [{"type": "image", "source": {"data": "x" * 50}}]}

    def test_is_settled_user_turn_rejects_continuation_accepts_real_boundary(self):
        messages = [
            {"role": "assistant", "content": "a" * 40},   # 0
            {"role": "user", "content": "b" * 40},         # 1  真干净:前一条是 assistant
            {"role": "assistant", "content": "c" * 40},    # 2
            self._tool_result_msg(),                        # 3  带 tool_result
            self._image_only_msg(),                         # 4  疑似漂移续片段:前一条是 user
            {"role": "assistant", "content": "e" * 40},    # 5
            {"role": "user", "content": "f" * 40},          # 6  真干净
        ]
        self.assertFalse(self.c._is_settled_user_turn(messages, 3))  # 本身带 tool_result
        self.assertFalse(self.c._is_settled_user_turn(messages, 4))  # 无 tool_result,但紧跟 user
        self.assertTrue(self.c._is_settled_user_turn(messages, 1))
        self.assertTrue(self.c._is_settled_user_turn(messages, 6))

    def test_is_settled_user_turn_accepts_index_zero_user(self):
        self.assertTrue(self.c._is_settled_user_turn([{"role": "user", "content": "hi"}], 0))

    def test_find_keep_index_skips_drifting_continuation(self):
        # max_idx = len-4 = 4,恰好等于漂移续片段的下标——_find_keep_index 自身会先被这个安全
        # 封顶夹回 4,但夹回前的「真实」目标(用旧逻辑会直接锁定的坐标)必须是更靠后的真干净边界,
        # 不能反过来在续片段上提前停手。
        messages = [
            {"role": "assistant", "content": "a" * 40},   # 0
            {"role": "user", "content": "b" * 40},         # 1
            {"role": "assistant", "content": "c" * 40},    # 2
            self._tool_result_msg(),                        # 3
            self._image_only_msg(),                         # 4  <- 不能落在这
            {"role": "assistant", "content": "e" * 40},    # 5
            {"role": "user", "content": "f" * 40},          # 6  <- 真干净边界
            {"role": "assistant", "content": "g" * 40},    # 7
        ]
        self.assertEqual(self.c._find_keep_index(messages, 0.425), 4)  # 安全封顶生效

    def test_select_cut_forward_scan_skips_drifting_continuation(self):
        # 与上一测试同一批消息:_find_keep_index 因安全封顶返回 4(恰好落在漂移续片段上),
        # 旧逻辑的 _select_cut 前向扫描会把 clean_idx 直接锁死在 4——本测试断言新逻辑会继续
        # 前扫,落到真正的干净边界 6,而不是被封顶坐标"卡死"在续片段中间。
        messages = [
            {"role": "assistant", "content": "a" * 40},   # 0
            {"role": "user", "content": "b" * 40},         # 1
            {"role": "assistant", "content": "c" * 40},    # 2
            self._tool_result_msg(),                        # 3
            self._image_only_msg(),                         # 4  <- 旧逻辑会切在这
            {"role": "assistant", "content": "e" * 40},    # 5
            {"role": "user", "content": "f" * 40},          # 6  <- 新逻辑落在这
            {"role": "assistant", "content": "g" * 40},    # 7
        ]
        start_idx, keep_from_idx, prefix_len = self.c._select_cut(messages, 0.425)
        self.assertEqual(keep_from_idx, 6)
        self.assertEqual(prefix_len, 2)

    def test_select_cut_backward_fallback_skips_drifting_continuation(self):
        # 前向扫描到尾都找不到干净/tool对边界(整段是 burst tail:全 user、无 assistant)时
        # 触发后向回退。回退路上先遇到一对漂移续片段(3,4),旧逻辑一到"无 tool_result 的 user"
        # 就地止步,会锁死在续片段 4 上;新逻辑必须穿过它,退到更早的真干净边界 1。
        messages = [
            {"role": "assistant", "content": "a" * 40},   # 0
            {"role": "user", "content": "b" * 40},         # 1  <- 期望回退落点
            {"role": "assistant", "content": "c" * 40},    # 2
            self._tool_result_msg(),                        # 3  漂移续片段 part1
            self._image_only_msg(),                         # 4  漂移续片段 part2 <- 旧逻辑会切在这
            {"role": "assistant", "content": "e" * 40},    # 5
            self._tool_result_msg(),                        # 6  burst tail 起点(全程无 assistant)
            self._tool_result_msg(),                        # 7
            self._image_only_msg(),                         # 8
            self._image_only_msg(),                         # 9
        ]
        start_idx, keep_from_idx, prefix_len = self.c._select_cut(messages, 0.3)
        self.assertEqual(keep_from_idx, 1)
        self.assertEqual(prefix_len, 2)


class SummaryUserIdentity(unittest.TestCase):
    """摘要请求 metadata.user_id:优先复用真实请求的 user_id(同会话恒定→摘要与主请求同设备/会话);
    真实请求无 metadata 时回退自造,且 device_id/session_id 进程稳定(同进程摘要共享同一标识)。"""

    def setUp(self):
        # 隔离进程级「最近真实 user_id」缓存,免得测试间互相污染(回退路径会读它)。
        self._orig_last = compressor._last_real_user_id
        compressor._last_real_user_id = None

    def tearDown(self):
        compressor._last_real_user_id = self._orig_last

    def _capture_body(self, client_meta):
        captured = {}

        class _CapConn:
            def request(self, method, path, body=None, headers=None):
                captured["body"] = body
            def getresponse(self):
                return _FakeResp(200, json.dumps(
                    {"content": [{"type": "text", "text": "S"}], "usage": {}}).encode())
            def close(self):
                pass

        c = compressor.RollingCompressor()
        c._conn = lambda: _CapConn()
        c._summarize_chunk("hi", "", {}, client_meta)
        return json.loads(captured["body"])["metadata"]["user_id"]

    def test_reuses_real_user_id_verbatim(self):
        # 真实请求带 user_id → 摘要请求原样复用(设备/会话全真实、同会话一致)。
        uid = self._capture_body({"user_id": "user_real_abc_session_xyz"})
        self.assertEqual(uid, "user_real_abc_session_xyz")

    def test_falls_back_to_cached_real_user_id(self):
        # 当次无 metadata,但进程内见过真实 user_id → 回退复用【真值】,不落自造兜底。
        compressor.remember_real_user_id("user_CACHEDreal_account_x_session_y")
        self.assertEqual(self._capture_body(None), "user_CACHEDreal_account_x_session_y")

    def test_fabricates_when_no_metadata_and_no_cache(self):
        # 无 client_meta 且无缓存真值 → 兜底自造:非 JSON、结构仿真(user_..._session_...)、进程稳定。
        uid = self._capture_body(None)
        self.assertFalse(uid.startswith("{"), "兜底不该再是一眼假的 JSON")
        self.assertTrue(uid.startswith("user_"))
        self.assertIn("_session_", uid)
        self.assertEqual(uid, compressor._CC_FALLBACK_USER_ID)

    def test_fabricated_identity_stable_across_calls(self):
        # 两次兜底调用的 user_id 必须完全一致(进程稳定,不再每次 uuid4)。
        self.assertEqual(self._capture_body(None), self._capture_body(None))

    def test_empty_or_nonstr_user_id_falls_back(self):
        # user_id 为空串 / 非字符串 / 非 dict → 一律回退(此处无缓存 → 兜底),不发出无效标识。
        fabricated = self._capture_body(None)
        self.assertEqual(self._capture_body({"user_id": ""}), fabricated)
        self.assertEqual(self._capture_body({"user_id": 123}), fabricated)
        self.assertEqual(self._capture_body({}), fabricated)
        self.assertEqual(self._capture_body("not-a-dict"), fabricated)

    def test_remember_ignores_empty_and_nonstr(self):
        # 只缓存非空字符串:空串 / 非字符串一律不污染缓存。
        compressor.remember_real_user_id("")
        self.assertIsNone(compressor._last_real_user_id)
        compressor.remember_real_user_id(123)
        self.assertIsNone(compressor._last_real_user_id)
        compressor.remember_real_user_id("user_ok")
        self.assertEqual(compressor._last_real_user_id, "user_ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
