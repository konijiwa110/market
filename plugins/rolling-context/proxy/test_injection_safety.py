"""Unit tests for the injection structural safety valve — _injection_is_safe.

Problem② regression guard: when a stored compression is injected as
`prefix + messages[match_end:]`, a boundary orphan tool_result makes
_validate_tool_pairs front-cut the whole prefix (summary included), or the splice
leaves an assistant tail the upstream treats as a prefill. Both degrade the next
tool_use into a text `<invoke>` and leak a "count"/continuation fragment.

_injection_is_safe(merged) is the stop-loss: it must reject any merged that does
NOT start with a summary(user) AND end with a user message, so the caller aborts
injection and passes the original through instead.

Run:  python -m unittest test_injection_safety     (from this proxy/ dir)
"""
import os
import tempfile
import unittest

# Isolate before importing server (same pattern as test_emergency): server's debug
# log uses expanduser("~") and config reads the state dir at import time.
_TMP = tempfile.mkdtemp(prefix="rc-inj-")
os.makedirs(os.path.join(_TMP, ".claude"), exist_ok=True)
os.environ["USERPROFILE"] = _TMP
os.environ["HOME"] = _TMP
os.environ["ROLLING_CONTEXT_STATE_DIR"] = _TMP
os.environ.setdefault("ANTHROPIC_BASE_URL", "https://example.invalid")

import server  # noqa: E402
from compressor import SUMMARY_MARKER  # noqa: E402


def _summary_msg():
    return {"role": "user", "content": f"{SUMMARY_MARKER}\nprior conversation summary"}


class InjectionIsSafe(unittest.TestCase):
    def test_well_formed_summary_then_user_tail_is_safe(self):
        merged = [
            _summary_msg(),
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": "next question"},
        ]
        self.assertTrue(server._injection_is_safe(merged))

    def test_tool_result_user_tail_is_safe(self):
        # tool_result blocks are role:"user" — a valid clean tail.
        merged = [
            _summary_msg(),
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
        ]
        self.assertTrue(server._injection_is_safe(merged))

    def test_missing_summary_marker_is_unsafe(self):
        # Prefix front-cut by _validate_tool_pairs: summary gone, first msg is a bare user.
        merged = [
            {"role": "user", "content": "no marker here"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "q"},
        ]
        self.assertFalse(server._injection_is_safe(merged))

    def test_head_not_user_is_unsafe(self):
        merged = [
            {"role": "assistant", "content": f"{SUMMARY_MARKER} wrong role"},
            {"role": "user", "content": "q"},
        ]
        self.assertFalse(server._injection_is_safe(merged))

    def test_assistant_tail_is_unsafe(self):
        # Assistant tail → upstream treats it as prefill → text <invoke> degradation.
        merged = [
            _summary_msg(),
            {"role": "assistant", "content": "partial answer"},
        ]
        self.assertFalse(server._injection_is_safe(merged))

    def test_system_tail_is_safe(self):
        # CC appends task reminders / IDE diagnostics as a trailing role:"system" message.
        # 1.20.0's `tail == user` check misjudged these as malformed and (with the entry
        # removal) caused the compression storm — only an assistant tail is a prefill risk.
        merged = [
            _summary_msg(),
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
            {"role": "system", "content": "The task tools haven't been used recently..."},
        ]
        self.assertTrue(server._injection_is_safe(merged))

    def test_non_string_summary_content_is_unsafe(self):
        # Block-list content at head can't carry the marker check reliably → reject.
        merged = [
            {"role": "user", "content": [{"type": "text", "text": SUMMARY_MARKER}]},
            {"role": "user", "content": "q"},
        ]
        self.assertFalse(server._injection_is_safe(merged))

    def test_empty_or_singleton_is_unsafe(self):
        self.assertFalse(server._injection_is_safe([]))
        self.assertFalse(server._injection_is_safe(None))
        self.assertFalse(server._injection_is_safe([_summary_msg()]))


if __name__ == "__main__":
    unittest.main()
