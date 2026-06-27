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


if __name__ == "__main__":
    unittest.main(verbosity=2)
