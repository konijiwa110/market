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


class StripContextManagementBeta(unittest.TestCase):
    """摘要请求一律剥 context-management beta:摘要体不带 thinking/context_management,透传该头
    曾诱发上游按 clear_thinking_20251015 策略校验 → 400「requires thinking to be enabled or
    adaptive」,压缩间歇性全灭(2026-07-08 实案)。"""

    def test_removes_context_management_only(self):
        # 事故形状:CC(fable, thinking adaptive)主请求的真实 beta 串形态
        h = {"anthropic-beta": "claude-code-20250219,oauth-2025-04-20,"
                               "context-management-2025-06-27,interleaved-thinking-2025-05-14"}
        compressor._strip_context_management_beta(h)
        self.assertEqual(h["anthropic-beta"],
                         "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14")

    def test_drops_header_when_only_context_management(self):
        h = {"anthropic-beta": "context-management-2025-06-27"}
        compressor._strip_context_management_beta(h)
        self.assertNotIn("anthropic-beta", h)

    def test_header_name_and_token_case_insensitive(self):
        h = {"Anthropic-Beta": "Context-Management-2025-06-27,foo-bar"}
        compressor._strip_context_management_beta(h)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
