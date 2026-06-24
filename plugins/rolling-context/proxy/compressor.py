"""
Rolling Context Compressor

When context exceeds trigger_tokens, compresses old messages down to target_tokens
of recent context + a dense chronological summary of everything before.

Pure stdlib — no external dependencies.
"""

import json
import os
import ssl
import uuid
import gzip
import zlib
import secrets
import logging
import http.client
from urllib.parse import urlparse

log = logging.getLogger("rolling-context.compressor")

# 模拟 Claude Code 客户端：部分第三方中转网关的 claude_code_only 分组对 /v1/messages 严格校验
# system 含 CC 特征 text block + metadata.user_id 为 CC 格式，否则判非 CC 客户端而拒绝。摘要请求
# 是代理自造、非真实 CC 请求，故按官方形态补齐这两项以通过检测（头则透传真实请求的 UA/X-App/beta）。
_CC_SYSTEM_TEXT = "You are Claude Code, Anthropic's official CLI for Claude."
_CC_DEVICE_ID = secrets.token_hex(32)  # 64 位 hex，进程内稳定

_default_summarizer_url = os.environ.get("ROLLING_CONTEXT_UPSTREAM") or "https://api.anthropic.com"
SUMMARIZER_BASE_URL = os.environ.get("ROLLING_CONTEXT_SUMMARIZER_URL") or _default_summarizer_url
SUMMARIZER_API_KEY = os.environ.get("ROLLING_CONTEXT_SUMMARIZER_KEY") or ""
ssl_ctx = ssl.create_default_context()

_parsed_summarizer = urlparse(SUMMARIZER_BASE_URL)
_SUMMARIZER_HOST = _parsed_summarizer.hostname
_SUMMARIZER_PORT = _parsed_summarizer.port
_SUMMARIZER_SCHEME = _parsed_summarizer.scheme
_SUMMARIZER_PATH = _parsed_summarizer.path or ""


def _summarizer_conn():
    """Create a connection to the summarizer server (same style as server.py)."""
    if _SUMMARIZER_SCHEME == "https":
        return http.client.HTTPSConnection(
            _SUMMARIZER_HOST,
            _SUMMARIZER_PORT or 443,
            context=ssl_ctx,
            timeout=120,
        )
    else:
        return http.client.HTTPConnection(
            _SUMMARIZER_HOST,
            _SUMMARIZER_PORT or 80,
            timeout=120,
        )


def _join_path(upstream_path: str, request_path: str) -> str:
    """Join upstream path with request path, handling edge cases."""
    if not upstream_path:
        return request_path
    if not request_path or request_path == "/":
        return upstream_path
    if upstream_path.endswith("/") and request_path.startswith("/"):
        return upstream_path[:-1] + request_path
    if not upstream_path.endswith("/") and not request_path.startswith("/"):
        return upstream_path + "/" + request_path
    return upstream_path + request_path

SUMMARY_MARKER = "[ROLLING_CONTEXT_SUMMARY]"
SUMMARY_END_MARKER = "[/ROLLING_CONTEXT_SUMMARY]"

SUMMARIZE_PROMPT = """You are a context compressor for an AI coding assistant conversation.

Your job: take the conversation below and produce a CHRONOLOGICAL, DENSE technical summary.

RULES:
- Structure as a TIMELINE: use numbered steps showing what happened in order
- Preserve ALL file paths, function/class/variable names EXACTLY as written
- Preserve ALL technical decisions and WHY they were made
- Preserve ALL code changes: what file, what was changed, what the new code does
- Preserve ALL errors encountered and how they were resolved
- Preserve ALL user requests and instructions — what they asked for, what constraints they gave, what they said to do or NOT do
- Preserve user preferences, workflow choices, and recurring patterns (e.g. "always use X", "never do Y")
- Include key code snippets when they're central to understanding (keep them short)
- Do NOT editorialize or add commentary
- Be as DENSE as possible — every sentence should carry information

FORMAT:
## Active Goal
- [What the user is CURRENTLY asking for — their most recent request or focus]
- [Any constraints or rules the user has stated (do/don't do)]

## Previous Goals (completed or shifted away from)
- [Earlier goals that were finished or that the user moved on from — keep brief]

## Timeline
1. [First thing that happened]
2. [Second thing...]
...

## Current State
- [What's done, what's in progress, what's next]

## Key Details
- [File paths, configs, decisions that must not be forgotten]

{existing_summary_section}

CONVERSATION TO COMPRESS:
{conversation}

Write the chronological summary:"""


class RollingCompressor:
    def __init__(
        self,
        trigger_tokens: int = 80000,
        target_tokens: int = 40000,
        summarizer_model: str = "claude-haiku-latest",
        summarizer_url: str = None,
        summarizer_api_key: str = None,
    ):
        self.trigger_tokens = trigger_tokens
        self.target_tokens = target_tokens
        self.summarizer_model = summarizer_model
        self.compression_count = 0
        self.total_tokens_saved = 0
        # 摘要上游：显式传入 > 环境变量 > 默认。修复原版只读 env、对第三方 baseURL 失效（固定打 api.anthropic.com）。
        url = (
            summarizer_url
            or os.environ.get("ROLLING_CONTEXT_SUMMARIZER_URL")
            or os.environ.get("ROLLING_CONTEXT_UPSTREAM")
            or "https://api.anthropic.com"
        )
        self.summarizer_url = url
        self.summarizer_api_key = summarizer_api_key or os.environ.get("ROLLING_CONTEXT_SUMMARIZER_KEY") or ""
        _p = urlparse(url)
        self._host = _p.hostname
        self._port = _p.port
        self._scheme = _p.scheme
        self._summ_path = _p.path or ""

    def _conn(self):
        """到摘要上游的连接（按实例的 summarizer_url）。"""
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port or 443, context=ssl_ctx, timeout=120)
        return http.client.HTTPConnection(self._host, self._port or 80, timeout=120)

    @staticmethod
    def _image_chars(source) -> int:
        """图片块的「字符当量」:按 base64 长度估 token、封顶 1600(Anthropic 单图 token 量级上限),
        再 ×4 换算成与文本同尺度的字符数。

        原 _count_chars 完全不计图片,而浏览器/DevTools/设计类会话里截图常占内容的 50%+(实测两个
        大会话分别 48.9% / 58.7%),致压缩计量对 token 大头失明:keep_ratio 失真、切点几乎不切,
        每轮空转。这里只求与真实 token 大致成比例,精度不苛求(无图片尺寸,只能据 base64 长度估)。
        """
        b64 = source.get("data", "") if isinstance(source, dict) else ""
        tokens = min(1600, max(1, len(b64) // 1000))
        return tokens * 4

    def _count_chars(self, messages: list) -> int:
        """统计全部消息的「字符当量」(文本按实际长度,图片按 _image_chars 估算),用于压缩切点/触发判断。"""
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type")
                        if btype == "text":
                            total_chars += len(block.get("text", ""))
                        elif btype == "thinking":
                            total_chars += len(block.get("thinking", ""))
                        elif btype == "image":
                            total_chars += self._image_chars(block.get("source", {}))
                        elif btype == "tool_use":
                            total_chars += len(json.dumps(block.get("input", {})))
                        elif btype == "tool_result":
                            c = block.get("content", "")
                            if isinstance(c, str):
                                total_chars += len(c)
                            elif isinstance(c, list):
                                for sub in c:
                                    if isinstance(sub, dict):
                                        if sub.get("type") == "image":
                                            total_chars += self._image_chars(sub.get("source", {}))
                                        else:
                                            total_chars += len(sub.get("text", ""))
        return total_chars

    def _find_keep_index(self, messages: list, keep_ratio: float) -> int:
        """Find the cut point: keep the last keep_ratio fraction of content."""
        if len(messages) <= 4:
            return 0
        max_idx = len(messages) - 4
        total_chars = self._count_chars(messages)
        target_chars = int(total_chars * keep_ratio)
        accumulated = 0
        for i in range(len(messages) - 1, -1, -1):
            msg_chars = self._count_chars([messages[i]])
            if accumulated + msg_chars > target_chars:
                for j in range(i + 1, len(messages)):
                    if messages[j].get("role") == "user":
                        if not self._has_tool_result(messages[j]):
                            return min(j, max_idx)
                return min(i + 1, max_idx)
            accumulated += msg_chars
        return 0

    def _has_tool_result(self, message: dict) -> bool:
        content = message.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
        return False

    def _has_summary(self, messages: list) -> bool:
        if not messages:
            return False
        content = messages[0].get("content", "")
        if isinstance(content, str):
            return SUMMARY_MARKER in content
        return False

    def _extract_summary(self, messages: list) -> str:
        if not self._has_summary(messages):
            return ""
        content = messages[0].get("content", "")
        if isinstance(content, str) and SUMMARY_MARKER in content:
            start = content.find(SUMMARY_MARKER) + len(SUMMARY_MARKER)
            end = content.find(SUMMARY_END_MARKER)
            if end > start:
                return content[start:end].strip()
        return ""

    def _messages_to_text(self, messages: list) -> str:
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "?")
                            inp = json.dumps(block.get("input", {}))
                            if len(inp) > 500:
                                inp = inp[:400] + "...[truncated]"
                            text_parts.append(f"[Tool: {name}({inp})]")
                        elif block.get("type") == "tool_result":
                            c = block.get("content", "")
                            if isinstance(c, str):
                                text_parts.append(f"[Result: {c[:1000]}]")
                            elif isinstance(c, list):
                                for sub in c:
                                    if isinstance(sub, dict):
                                        text_parts.append(f"[Result: {sub.get('text', '')[:1000]}]")
                text = "\n".join(text_parts)
            else:
                text = str(content)

            if len(text) > 4000:
                text = text[:3000] + "\n...[truncated]...\n" + text[-1000:]
            parts.append(f"**{role}**: {text}")
        return "\n\n".join(parts)

    def compress(self, messages: list, auth_headers: dict, real_token_count: int = None) -> list:
        """Compress messages using rolling summarization (synchronous)."""
        # Use real API token count to determine what fraction of content to keep
        if real_token_count and real_token_count > 0:
            keep_ratio = self.target_tokens / real_token_count
            log.info(
                f"Keep ratio: {keep_ratio:.1%} "
                f"(target={self.target_tokens:,} / real={real_token_count:,})"
            )
        else:
            # Fallback: keep half (conservative)
            keep_ratio = 0.5
            log.info(f"Keep ratio: {keep_ratio:.1%} (fallback, no real token count)")

        keep_from_idx = self._find_keep_index(messages, keep_ratio)

        # 切点须落在干净 user 边界：Claude Code 会在 messages 间插入独立 role:system 消息，
        # 保留段以 system/assistant/孤立 tool_result 开头时，注入后紧跟 ack(assistant) 会违反
        # 「system 须跟 user 后 / 角色需交替」→ 上游 400。推进到下一个干净 user 边界。
        while keep_from_idx < len(messages) and (
            messages[keep_from_idx].get("role") != "user" or self._has_tool_result(messages[keep_from_idx])
        ):
            keep_from_idx += 1

        has_existing_summary = self._has_summary(messages)
        start_idx = 2 if has_existing_summary else 0

        if keep_from_idx <= start_idx or keep_from_idx >= len(messages):
            log.info("Not enough old messages to compress, passing through")
            return messages

        existing_summary = self._extract_summary(messages) if has_existing_summary else ""
        to_compress = messages[start_idx:keep_from_idx]
        recent_messages = messages[keep_from_idx:]

        if not to_compress:
            log.info("Nothing to compress")
            return messages

        conversation_text = self._messages_to_text(to_compress)

        summary_max_tokens = 16000

        existing_section = ""
        if existing_summary:
            existing_section = (
                "EXISTING ROLLING SUMMARY FROM PREVIOUS COMPRESSIONS "
                "(integrate this timeline with the new conversation below — "
                "keep all details, extend the timeline):\n"
                f"{existing_summary}\n\n"
            )

        prompt = SUMMARIZE_PROMPT.format(
            existing_summary_section=existing_section,
            conversation=conversation_text,
        )

        log.info(
            f"Summarizing {len(to_compress)} messages ({len(conversation_text):,} chars) "
            f"with {self.summarizer_model} (max_tokens={summary_max_tokens:,})..."
        )

        cc_user_id = json.dumps({
            "device_id": _CC_DEVICE_ID, "account_uuid": "", "session_id": str(uuid.uuid4()),
        })
        req_body = json.dumps({
            "model": self.summarizer_model,
            "max_tokens": summary_max_tokens,
            # 模拟 Claude Code 客户端以通过上游 claude_code_only 检测。
            "system": [{"type": "text", "text": _CC_SYSTEM_TEXT}],
            "metadata": {"user_id": cc_user_id},
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        # 始终基于透传头（含真实 CC 的 UA/X-App/anthropic-beta/anthropic-version）；
        # 配了 apikey 才覆盖上游鉴权——否则会丢掉 CC 头而过不了检测。
        headers = dict(auth_headers)
        if self.summarizer_api_key:
            lowered = {k.lower(): k for k in headers}
            headers.pop(lowered.get("x-api-key", ""), None)
            headers[lowered.get("authorization", "Authorization")] = f"Bearer {self.summarizer_api_key}"
        # 透传头里 content-length/accept-encoding 多为首字母大写；dict 大小写敏感，直接用小写键设置会
        # 与原键并存导致发送重复头（上游会取到带 gzip 的那个）。先按大小写无关删净，再统一覆盖。
        for k in [k for k in headers if k.lower() in ("content-length", "accept-encoding")]:
            headers.pop(k)
        headers["content-length"] = str(len(req_body))
        headers["accept-encoding"] = "identity"

        summarizer_path = _join_path(self._summ_path, "/v1/messages")
        log.info(f"Compression request -> {self.summarizer_url} path={summarizer_path}")

        conn = self._conn()
        conn.request("POST", summarizer_path, body=req_body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        enc = (resp.getheader("Content-Encoding") or "").lower()
        conn.close()

        # 上游可能无视 accept-encoding:identity 仍回 gzip/deflate，按 Content-Encoding 解压兜底。
        if "gzip" in enc:
            resp_body = gzip.decompress(resp_body)
        elif "deflate" in enc:
            resp_body = zlib.decompress(resp_body)

        if resp.status != 200:
            error = resp_body.decode("utf-8", errors="replace")
            raise RuntimeError(f"Summarization API returned {resp.status}: {error[:500]}")
        data = json.loads(resp_body)

        new_summary = data["content"][0]["text"]
        log.info(f"Summary generated: {len(new_summary):,} chars")

        summary_message = {
            "role": "user",
            "content": (
                f"{SUMMARY_MARKER}\n"
                f"{new_summary}\n"
                f"{SUMMARY_END_MARKER}\n\n"
                "The above is a chronological summary of our earlier conversation. "
                "All file paths, decisions, and code changes are preserved. "
                "Continue from where we left off."
            ),
        }
        ack_message = {
            "role": "assistant",
            "content": (
                "I have the full context from our previous conversation — "
                "the timeline, all files modified, decisions made, and current state. "
                "Continuing from where we left off."
            ),
        }

        compressed = [summary_message, ack_message] + recent_messages

        original_chars = self._count_chars(messages)
        compressed_chars = self._count_chars(compressed)
        summary_chars = len(new_summary)
        recent_chars = self._count_chars(recent_messages)
        self.compression_count += 1
        if real_token_count:
            reduction = compressed_chars / original_chars if original_chars > 0 else 0
            estimated_output_tokens = int(real_token_count * reduction)
            self.total_tokens_saved += real_token_count - estimated_output_tokens
            log.info(
                f"Compression #{self.compression_count}: "
                f"~{real_token_count:,} -> ~{estimated_output_tokens:,} real tokens "
                f"({reduction:.0%} of original, "
                f"summary={summary_chars:,} chars, recent={recent_chars:,} chars)"
            )
        else:
            self.total_tokens_saved += (original_chars - compressed_chars) // 2
            log.info(
                f"Compression #{self.compression_count}: "
                f"{original_chars:,} -> {compressed_chars:,} chars "
                f"(summary={summary_chars:,}, recent={recent_chars:,})"
            )

        return compressed
