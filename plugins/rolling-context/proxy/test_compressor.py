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


if __name__ == "__main__":
    unittest.main(verbosity=2)
