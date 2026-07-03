"""
Claude Rolling Context Proxy

A transparent proxy between Claude Code and the Anthropic API.
Compresses old messages in the background using Haiku, keeping recent messages
verbatim. Zero latency — compression runs async, applied on the next request.

Uses content-based matching: hashes each message, recognizes previously compressed
messages by their content, and replaces them with the compressed version.
No sessions, no fingerprints — just content recognition.

Pure stdlib — no external dependencies needed.
"""

import hashlib
import json
import os
import sys
import gzip
import time
import logging
import logging.handlers
import threading
import ssl
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from compressor import RollingCompressor, SUMMARY_MARKER
from stats import StatsCollector

class FlushFileHandler(logging.handlers.RotatingFileHandler):
    """每条日志即时 flush 落盘;按大小滚动,封顶总占用,避免长期运行把磁盘写爆。"""
    def emit(self, record):
        super().emit(record)
        self.flush()


# 会话级日志:多会话共用一个代理进程、一份日志,扁平日志无法区分是哪个会话、停在哪一步。
# 用线程本地保存「会话标签」,再用 Filter 把它注入每条日志记录——这样所有日志(含 [BG]/[MATCH]/
# 压缩器子 logger)都自动带上标签,无需逐条改 log 调用。
_sess_ctx = threading.local()


def _set_sess(tag: str):
    _sess_ctx.tag = tag


def _get_sess() -> str:
    return getattr(_sess_ctx, "tag", "--------")


class _SessionFilter(logging.Filter):
    """把当前线程的会话标签与短线程 id 注入日志记录,供 formatter 使用。"""
    def filter(self, record):
        record.sess = _get_sess()
        record.tid = threading.get_ident() % 100000
        return True


_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(sess)s|t%(tid)05d] %(message)s"
_sess_filter = _SessionFilter()

_log_path = os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-debug.log")
# 日志滚动:单文件最多 10MB,保留 5 个历史(rolling-context-debug.log.1..5),总占用 ≈60MB 封顶,
# 防止长期运行把磁盘写爆。可用环境变量覆写(ROLLING_CONTEXT_LOG_MB / _LOG_BACKUPS)。
_LOG_MAX_BYTES = int(os.environ.get("ROLLING_CONTEXT_LOG_MB", "10")) * 1024 * 1024
_LOG_BACKUPS = int(os.environ.get("ROLLING_CONTEXT_LOG_BACKUPS", "5"))
_log_handler = FlushFileHandler(
    _log_path, mode="a", maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8",
)
_log_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_log_handler.addFilter(_sess_filter)
# stdout(被 start-proxy 重定向到 rolling-context-proxy.log)只收 INFO+,体量约为 DEBUG 的十分之一;
# 完整 DEBUG 仍写入上面会滚动的 debug.log。两边都不再无界增长。
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_stream_handler.addFilter(_sess_filter)
_stream_handler.setLevel(logging.INFO)
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[_stream_handler, _log_handler],
)
log = logging.getLogger("rolling-context")

def _load_config() -> dict:
    """读取 ~/.claude/rolling-context.json：第三方 baseURL 与压缩参数的显式、稳定来源。"""
    try:
        p = os.path.join(os.path.expanduser("~"), ".claude", "rolling-context.json")
        # utf-8-sig：容忍 BOM。Windows PowerShell 写出的 json 常带 UTF8 BOM，
        # 纯 utf-8 读会抛 JSONDecodeError 被吞掉，导致配置/上游解析失败。
        with open(p, encoding="utf-8-sig") as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except Exception:
        return {}


CONFIG = _load_config()


def _cfg(key: str, env_key: str, default):
    """配置优先级：rolling-context.json > 环境变量 > 默认值。"""
    v = CONFIG.get(key)
    if v not in (None, ""):
        return v
    v = os.environ.get(env_key)
    return v if v else default


LISTEN_PORT = int(_cfg("port", "ROLLING_CONTEXT_PORT", 5588))
# 监听地址:默认仅回环(127.0.0.1,安全)。设 ROLLING_CONTEXT_HOST=0.0.0.0 可让其它设备连接,
# 但代理会带 ANTHROPIC_AUTH_TOKEN 转发——开放后务必限可信内网,勿暴露公网。
LISTEN_HOST = str(_cfg("host", "ROLLING_CONTEXT_HOST", "127.0.0.1"))


def _plugin_version() -> str:
    """本进程跑的插件版本：读同源 ../.claude-plugin/plugin.json（与代码同处一份,绝不漂移）。
    供 /health 自报 + 写权威 version 文件,让 hook 的版本闸门直接问活着的代理,不再靠猜。"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".claude-plugin", "plugin.json")
        with open(p, encoding="utf-8-sig") as f:
            v = (json.load(f) or {}).get("version")
            if v:
                return str(v)
    except Exception:
        pass
    return os.environ.get("ROLLING_CONTEXT_VERSION", "unknown")


VERSION = _plugin_version()
# 状态目录默认 ~/.claude;可用 ROLLING_CONTEXT_STATE_DIR 覆盖,让冒烟测试写到 tmp 目录、
# 不污染用户真实 pidfile/version(否则跑一遍测试就把活着的 5588 网关记号冲掉)。
_CLAUDE_DIR = os.environ.get("ROLLING_CONTEXT_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
PID_FILE = os.path.join(_CLAUDE_DIR, "rolling-context-proxy.pid")
VER_FILE = os.path.join(_CLAUDE_DIR, "rolling-context-proxy.version")


def _load_upstream() -> str:
    """Resolve the upstream API endpoint.

    Prefer ROLLING_CONTEXT_UPSTREAM from the environment. The hook writes it into
    settings.json but does not export it into this process (issue #3), so fall
    back to reading settings.json directly — this is what lets the proxy work
    with custom endpoints (DeepSeek, OpenRouter, a local proxy, a chained PII
    proxy) instead of always hitting api.anthropic.com.
    """
    up = CONFIG.get("upstream")
    if up:
        return up
    up = os.environ.get("ROLLING_CONTEXT_UPSTREAM")
    if up:
        return up
    try:
        settings_path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
        # utf-8-sig：settings.json 可能由 PowerShell 写出带 BOM，纯 utf-8 会解析失败
        # 而退回 api.anthropic.com（用第三方 token 打 Anthropic → 403）。
        with open(settings_path, encoding="utf-8-sig") as f:
            env_vars = (json.load(f) or {}).get("env", {}) or {}
        up = env_vars.get("ROLLING_CONTEXT_UPSTREAM")
        if up:
            return up
        # Last resort: a custom ANTHROPIC_BASE_URL — but never route back at
        # ourselves (that would loop).
        base = env_vars.get("ANTHROPIC_BASE_URL", "")
        if base and (urlparse(base).port or 0) != LISTEN_PORT:
            return base
    except Exception:
        pass
    return "https://api.anthropic.com"


UPSTREAM_URL = _load_upstream()
TRIGGER_TOKENS = int(_cfg("trigger", "ROLLING_CONTEXT_TRIGGER", 160000))
TARGET_TOKENS = int(_cfg("target", "ROLLING_CONTEXT_TARGET", 40000))
# 真实上下文窗口判定:代理只看请求体,本不知模型窗口是 200k 还是 1M。CC 用 model[1m] 时会带
# anthropic-beta: context-1m-... 头 → 据此每请求确定性判窗口。用户把 trigger 配超真实窗口(如为
# 1M 调到 320k 但实际只 200k)时,主动压缩永不触发、撞墙吃 400;有效 trigger 夹到「窗口×安全余量」
# 之下即可在撞墙前主动压。第三方上游可能谎报 1M(发了头实际 200k),用 context_window 显式钉死覆盖。
WINDOW_1M = 1_000_000
WINDOW_DEFAULT = 200_000
TRIGGER_SAFETY = 0.9            # 有效 trigger 占真实窗口的比例(留 10% 给单轮突发增长 + emergency 兜底)
_BETA_1M_MARK = "context-1m"   # 子串匹配,对日期后缀(context-1m-2025-08-07)变化稳健
CONTEXT_WINDOW_OVERRIDE = int(_cfg("context_window", "ROLLING_CONTEXT_CONTEXT_WINDOW", 0))  # 0=未设,走头判定
SUMMARIZER_MODEL = _cfg("model", "ROLLING_CONTEXT_MODEL", "claude-haiku-4-5-20251001")
# 鉴权：config 显式给了才用，否则透传 claude 发来的 ANTHROPIC_AUTH_TOKEN（默认不写）。
APIKEY = _cfg("apikey", "ROLLING_CONTEXT_APIKEY", "")
# 永不超发兜底:未命中缓存时若上游以「prompt too long」拒绝超限请求,就同步压一次并重试一发,
# CC 端永不看到这发 400(也让 CC 自己的 autoCompact/`/compact` 那发超限摘要请求被压住、得以成功,
# 把真实 transcript 焊小 → 停插件后也不再全量重跑)。默认开;设 0/false/off 关闭回到旧的裸发行为。
EMERGENCY_COMPRESS = str(_cfg("emergency_compress", "ROLLING_CONTEXT_EMERGENCY_COMPRESS", "1")).lower() not in ("0", "false", "off", "no")
# 主动同步压缩(消除 resume 冷启动卡顿):未命中缓存的大请求,在「转发上游之前」就按请求体大小估算 token,
# 若超有效 trigger 则当场同步压一次、把压缩结果换进请求体再转发——而非先全量发、再后台压(那样第一发要
# 等慢上游嚼完整全量 transcript,且账单按全量计)。压完即登记进 store,后续请求直接命中、不再付同步延迟。
# 与 EMERGENCY_COMPRESS 正交:emergency 是上游 400 后的被动兜底,proactive 是 400 之前的主动预压。
# 默认开;设 0/false/off 关闭,回到「先全量发、后台压」的旧行为。
PROACTIVE_COMPRESS = str(_cfg("proactive_compress", "ROLLING_CONTEXT_PROACTIVE_COMPRESS", "1")).lower() not in ("0", "false", "off", "no")

# 客户端伪装增强:代理自发请求(后台压缩 / emergency 兜底)默认套用「最近一个超 TARGET 大请求」的完整
# 真实头(UA/x-app/x-stainless-*/anthropic-* 等)作伪装模板,鉴权仍用当次请求的,以更稳地通过上游
# claude_code_only 检测。默认开;设 0/false/off 退回「用当次触发请求透传头」的旧行为。
DISGUISE_CLIENT = str(_cfg("disguise_client", "ROLLING_CONTEXT_DISGUISE", "1")).lower() not in ("0", "false", "off", "no")
# 伪装模板:进程级缓存最近一个大请求的伪装头(读写跨线程,_disguise_lock 保护)。鉴权与逐请求易变的连接/
# 编码头不进模板(content-length/accept-encoding 由 compressor 发送时各自重设)。
_disguise_lock = threading.Lock()
_disguise_template = None  # dict | None
_DISGUISE_SKIP = ("authorization", "x-api-key", "host", "content-length", "transfer-encoding", "accept-encoding")

# 大请求归档(便于事后审查「它到底输出了啥」):对大输出或长耗时的回合,把完整请求体 + 完整响应内容
# 单独落一份 gzip 存档。透传字节流不动,只读 buffer 副本;归档在响应已全量回给 CC、落库之前触发,
# 失败绝不影响请求。总量滚动封顶防无界增长。默认开;设 0/false/off 关闭。
ARCHIVE = str(_cfg("archive", "ROLLING_CONTEXT_ARCHIVE", "1")).lower() not in ("0", "false", "off", "no")
# 归档触发阈值:输出 token 数 ≥ MIN_OUT 或 总耗时(ms)≥ MIN_MS,任一即归档。两者皆可配。
ARCHIVE_MIN_OUT = int(_cfg("archive_min_out", "ROLLING_CONTEXT_ARCHIVE_MIN_OUT", 8000))
ARCHIVE_MIN_MS = int(_cfg("archive_min_ms", "ROLLING_CONTEXT_ARCHIVE_MIN_MS", 90000))
# 归档目录总量上限(MB):每次写入后若超限,按修改时间删最旧直到回落到上限之下。
ARCHIVE_CAP_MB = int(_cfg("archive_cap_mb", "ROLLING_CONTEXT_ARCHIVE_CAP_MB", 200))

ssl_ctx = ssl.create_default_context()
_parsed_upstream = urlparse(UPSTREAM_URL)
UPSTREAM_PATH = _parsed_upstream.path or ""


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


compressor = RollingCompressor(
    trigger_tokens=TRIGGER_TOKENS,
    target_tokens=TARGET_TOKENS,
    summarizer_model=SUMMARIZER_MODEL,
    summarizer_url=UPSTREAM_URL,  # 摘要走同一上游（修复原版固定打 api.anthropic.com，对第三方 baseURL 失效）
    summarizer_api_key=APIKEY or None,  # 空则压缩器透传 claude 的鉴权（从环境读）
)


def _upstream_conn():
    """Create a connection to the upstream server."""
    if _parsed_upstream.scheme == "https":
        return http.client.HTTPSConnection(
            _parsed_upstream.hostname,
            _parsed_upstream.port or 443,
            context=ssl_ctx,
            timeout=600,
        )
    else:
        return http.client.HTTPConnection(
            _parsed_upstream.hostname,
            _parsed_upstream.port or 80,
            timeout=600,
        )


# ---------------------------------------------------------------------------
# Content-based matching
# ---------------------------------------------------------------------------

import re

_VOLATILE_TAGS_RE = re.compile(
    r"<(?:system-reminder|local-command-caveat|local-command-stdout|"
    r"available-deferred-tools)>.*?</(?:system-reminder|local-command-caveat|"
    r"local-command-stdout|available-deferred-tools)>",
    re.DOTALL,
)


def _strip_volatile_tags(text: str) -> str:
    """Strip Claude Code's dynamic tags that change between requests."""
    return _VOLATILE_TAGS_RE.sub("", text)


def _normalize_content(content):
    """Strip volatile metadata (cache_control, system-reminder) for stable hashing."""
    if isinstance(content, str):
        return _strip_volatile_tags(content)
    if isinstance(content, list):
        result = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in ("thinking", "redacted_thinking"):
                    continue  # CC resume 时会剥掉历史 thinking 块,哈希必须对其免疫,否则恢复会话后所有深条目失配
                b = {}
                for k, v in block.items():
                    if k == "cache_control":
                        continue
                    if k == "content" and isinstance(v, (list, str)):
                        b[k] = _normalize_content(v)
                    elif k == "text" and isinstance(v, str):
                        b[k] = _strip_volatile_tags(v)
                    else:
                        b[k] = v
                result.append(b)
            else:
                result.append(block)
        return result
    return content


def _hash_message(msg: dict) -> str:
    """Stable hash of a message, ignoring cache_control metadata."""
    role = msg.get("role", "")
    content = _normalize_content(msg.get("content", ""))
    if not isinstance(content, str):
        content = json.dumps(content, sort_keys=True)
    raw = f"{role}:{content}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _hash_messages(messages: list) -> list:
    return [_hash_message(m) for m in messages]


# ---------------------------------------------------------------------------
# 永不超发:同步压缩兜底的判定 / 解析 / 复用辅助
# ---------------------------------------------------------------------------

def _looks_too_long(body: bytes) -> bool:
    """上游 400 响应体是否为「提示词超过上下文上限」类错误(如 'prompt is too long: N tokens >
    1000000 maximum')。只认这一类才触发同步压缩重试,其它 400(鉴权/格式)原样回给 CC。"""
    try:
        s = body.decode("utf-8", "replace").lower()
    except Exception:
        return False
    if "too long" in s:
        return True
    return ("maximum" in s and "token" in s)


_REPORTED_TOK_RE = re.compile(r"(\d[\d,]{3,})\s*tokens?", re.I)


def _parse_reported_tokens(body: bytes):
    """从超限错误体里解析出「这次提示词的真实 token 数」(错误串里 'tokens' 前的第一个大数,
    如 2100398),用作压缩 keep 比例的分母 → 一次就压到上限内。解析不到返回 None。"""
    try:
        s = body.decode("utf-8", "replace")
    except Exception:
        return None
    m = _REPORTED_TOK_RE.search(s)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _compression_key_hashes(source_messages: list, compressed: list, prefix_len: int):
    """由「压缩前的源消息」与「压缩结果」反推被摘要掉那段的 hash 链(匹配 key)。
    返回 (key_hashes, summarized_slice)。后台压缩与同步兜底共用,保证两条路径产出的 key 一致。"""
    recent_count = len(compressed) - prefix_len
    summarized = source_messages[:len(source_messages) - recent_count]
    start = 0
    if summarized and isinstance(summarized[0].get("content", ""), str):
        if SUMMARY_MARKER in summarized[0]["content"]:
            start = 2
    return _hash_messages(summarized[start:]), summarized[start:]


def _remark_cache_breakpoints(msgs: list):
    """先删净所有 cache_control(注入/重组后旧断点位置失效、且可能超 4 个上限),再在两个稳定边界
    各打一个 ephemeral:前缀末尾(摘要+ack,跨轮稳定)与末条消息(近端尾巴 5min 窗口内 cache_read)。
    断点总数 = system + tools + 2 ≤ 4,不触发上游 400。注入路径与同步兜底共用。"""
    for m in msgs:
        c = m.get("content", "")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
    if len(msgs) >= 2:
        _mark_cache_breakpoint(msgs[1])
    if msgs:
        _mark_cache_breakpoint(msgs[-1])


# 压缩成果落盘:store 原本只在内存,代理一重启(版本闸门/refresh/重启/崩溃自拉)或新开
# --resume 长会话时 store 为空 → 第一发裸转全量历史(异步压缩只能为「下一发」备料,救不了「这一发」)。
# 持久化后,重启/恢复时 store 是热的,长会话首发直接命中、不再满历史发送。
STORE_FILE = os.path.join(_CLAUDE_DIR, "rolling-context-store.json")
# 落盘保留的最近压缩条数上限,防止文件无界增长(越晚的条目覆盖历史越多)。回收父条目后每会话只剩 1 条
# 活条目,40 即 40 条并发血脉,足够任何现实并发;多会话超高并发可用 ROLLING_CONTEXT_STORE_MAX 调大。
STORE_MAX_ENTRIES = int(_cfg("store_max_entries", "ROLLING_CONTEXT_STORE_MAX", 40))


class CompressionStore:
    """Content-based compression tracking. No sessions, no fingerprints, no keys.

    Stores a list of compressions. Each has original_hashes (what was compressed)
    and prefix (the replacement). On ANY request, scans messages — if the hashes
    match a stored compression, replaces them with the prefix.

    成果落盘:可用条目(有 prefix + 哈希链)持久化到 STORE_FILE,重启/恢复后加载回来,
    消除冷启动时的满历史发送。内容哈希自校验 → 陈旧条目滑不中就是不被使用,绝不注入错的。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._compressions = []  # list of compression entries
        self._load()

    def _load(self):
        """启动时从盘加载已落盘的可用压缩条目。文件缺失/损坏一律当空开始(失败开放,绝不挡启动)。"""
        try:
            with open(STORE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning(f"[STORE] Could not load persisted store ({e}); starting empty")
            return
        n = 0
        for d in (data.get("entries") or []):
            oh = d.get("original_hashes") or []
            prefix = d.get("prefix")
            if not oh or not prefix:
                continue  # 只收完整可用的条目;残缺的丢弃
            self._compressions.append({
                "original_hashes": oh,
                "prefix": prefix,
                "pending": None,
                "pending_hashes": None,
                "intent_hashes": None,
                "thread": None,
                "used": bool(d.get("used", True)),
                "pre_tokens": int(d.get("pre_tokens", 0) or 0),
            })
            n += 1
        if n:
            log.info(f"[STORE] Loaded {n} persisted compression(s) from {STORE_FILE}")

    def persist(self):
        """把当前可用条目原子写盘(临时文件 + os.replace,防崩溃半写)。
        只存 prefix + 哈希链 + used/pre_tokens;pending/thread/_debug_messages 是运行期状态,不落盘。"""
        with self._lock:
            usable = [e for e in self._compressions
                      if e.get("prefix") and e.get("original_hashes")][-STORE_MAX_ENTRIES:]
            entries = [{
                "original_hashes": e["original_hashes"],
                "prefix": e["prefix"],
                "used": e.get("used", True),
                "pre_tokens": e.get("pre_tokens", 0),
            } for e in usable]
        try:
            os.makedirs(_CLAUDE_DIR, exist_ok=True)
            tmp = STORE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "entries": entries}, f, ensure_ascii=False)
            os.replace(tmp, STORE_FILE)
        except Exception as e:
            log.warning(f"[STORE] Could not persist store: {e}")

    def find_match(self, msg_hashes: list, messages: list = None):
        """Find a compression whose hash chain appears in msg_hashes.

        Returns the match whose chain ends furthest into the request
        (latest compression = covers the most history).
        Replaces everything up to and including the match, since the
        compression already contains a summary of everything before it.
        """
        with self._lock:
            best = None
            best_end = -1  # position in msg_hashes where the match ends
            for entry in self._compressions:
                oh = entry["original_hashes"]
                if not oh:
                    continue
                # Search for the hash chain in msg_hashes
                chain_len = len(oh)
                for start in range(len(msg_hashes) - chain_len + 1):
                    if msg_hashes[start:start + chain_len] == oh:
                        end = start + chain_len
                        if end > best_end:
                            best = entry
                            best_end = end
                        break
            # 整体一条都没命中才记日志,且降到 debug:store 全局共享,多会话并存时「某条压缩与本请求
            # 不匹配」是常态。原先逐条 warning 会按条数刷爆两个 handler、还拖慢热路径。
            if best is None and self._compressions and log.isEnabledFor(logging.DEBUG):
                self._log_no_match(msg_hashes, messages)
            return best, best_end

    def _log_no_match(self, msg_hashes: list, messages: list):
        """未命中诊断(debug 级,仅在整体无命中时调用一次):挑第一条存有原文的候选,
        打印其哈希链首个失配位置两端的内容,定位「内容为何漂移」。已在 find_match 的锁内调用。"""
        for entry in self._compressions:
            oh = entry.get("original_hashes") or []
            dbg = entry.get("_debug_messages")
            if not oh or not dbg:
                continue
            idx = next((i for i in range(min(len(oh), len(msg_hashes))) if oh[i] != msg_hashes[i]), None)
            if idx is None:
                continue
            stored = dbg[idx] if idx < len(dbg) else None
            incoming = messages[idx] if messages and idx < len(messages) else None
            if stored and incoming:
                log.debug(
                    f"[MATCH] No match among {len(self._compressions)} stored; first diff at [{idx}] "
                    f"role={stored.get('role')}:\n  STORED:   {str(stored.get('content', ''))[:300]}\n"
                    f"  INCOMING: {str(incoming.get('content', ''))[:300]}"
                )
                return
        log.debug(f"[MATCH] No match among {len(self._compressions)} stored compression(s)")

    def covers(self, msg_hashes: list, exclude=None) -> bool:
        """库里是否已有压缩其哈希链出现在 msg_hashes 中(排除 exclude 指向的条目)。
        检查顺序:original_hashes(已转正) → pending_hashes(待转正) → intent_hashes(刚声明意图),
        三者覆盖从触发到转正的全生命周期。"""
        with self._lock:
            return self._covers_locked(msg_hashes, exclude)

    def _covers_locked(self, msg_hashes: list, exclude=None, in_flight_only=False) -> bool:
        """covers 的无锁实现(调用者须已持 self._lock)。供 covers 与 claim_compression 复用,
        使「检查去重 → 登记意图」能在同一临界区内原子完成。

        in_flight_only=True 时只认「在途」压缩(intent_hashes / pending_hashes),忽略已转正的
        original_hashes。claim_compression 走这条:已转正条目归 find_match 管——若本发注入了它却仍
        超 trigger,恰恰证明需要「更深一层」的压缩,已转正条目绝不该把它挡下(否则会话只能一路涨到
        CC autocompact 兜底)。只有「同段压缩已在途」才是真冗余,才拦。默认 False 保持 covers() 全生命
        周期语义不变。"""
        for entry in self._compressions:
            if entry is exclude:
                continue
            if in_flight_only:
                oh = entry.get("pending_hashes") or entry.get("intent_hashes")
            else:
                oh = entry["original_hashes"] or entry.get("pending_hashes") or entry.get("intent_hashes")
            if not oh:
                continue
            chain_len = len(oh)
            for start in range(len(msg_hashes) - chain_len + 1):
                if msg_hashes[start:start + chain_len] == oh:
                    return True
        return False

    def _new_entry(self) -> dict:
        return {
            "original_hashes": [],   # hashes of original messages we replaced
            "prefix": None,          # compressed replacement messages
            "pending": None,         # pending compression result
            "pending_hashes": None,  # hashes for pending
            "intent_hashes": None,   # 触发时立即写入的原始 msg_hashes;pending_hashes 写入后清空
            "thread": None,          # background compression thread
            "used": False,           # 是否已被某个请求注入过(用于标记「压缩生效的第一个请求」)
            "pre_tokens": 0,         # 触发本次压缩时的上下文 token 数(压缩前规模,用于展示收缩效果)
            "parent": None,          # 本压缩建立其上的父条目(同会话上一次压缩);转正时回收,避免死条目堆积
        }

    def add(self) -> dict:
        entry = self._new_entry()
        with self._lock:
            self._compressions.append(entry)
            self._prune_locked()
        return entry

    def claim_compression(self, msg_hashes: list, exclude=None, in_flight_only=False):
        """原子地「检查去重 + 登记压缩意图」:在一把锁内先查是否已被覆盖,没有才新建条目、登记
        intent_hashes 并入库,返回该条目;已覆盖则返回 None。把「检查→登记」收进同一临界区,杜绝两个
        并发请求各自重复触发同一段压缩——此前靠全局 already_compressing 粗粒度兜,既不精确(TOCTOU
        仍漏)又会跨会话误挡。

        in_flight_only 由调用方按「本发是否已注入压缩」决定:
        - 本发已注入(injected_via 非空):find_match 已把【最深】的覆盖条目注入了,却仍 > trigger,
          说明真需要更深一层;此时 in_flight_only=True,已转正条目(original_hashes)不得挡下,只让
          【在途】压缩(intent/pending)去重。否则长会话被自身旧压缩挡住、深压缩永不建立,一路涨到
          CC autocompact 兜底(1.19.x 回归)。
        - 本发未注入(injected_via 为空):在途期间可能落地一条覆盖本段的压缩(下一发 find_match 会用
          它),此时 in_flight_only=False,已转正条目也算覆盖 → 跳过,避免与那条近乎重复。"""
        with self._lock:
            if self._covers_locked(msg_hashes, exclude=exclude, in_flight_only=in_flight_only):
                return None
            entry = self._new_entry()
            entry["intent_hashes"] = list(msg_hashes)
            self._compressions.append(entry)
            self._prune_locked()
            return entry

    def _prune_locked(self):
        """内存表封顶(已持锁):落盘/加载只在两端裁剪,内存里的 _compressions 原本只增不减,
        emergency 兜底与跨会话残留会让 find_match 的全表线性扫描越来越慢。超过 STORE_MAX_ENTRIES 时
        丢弃最老的空闲条目;正在后台压缩(thread 仍 alive)的一律保留,避免删掉马上要转正的成果。"""
        if len(self._compressions) <= STORE_MAX_ENTRIES:
            return

        def busy(e):
            t = e.get("thread")
            return (t is not None and t.is_alive()) or e.get("intent_hashes") is not None or e.get("pending") is not None

        alive = [e for e in self._compressions if busy(e)]
        idle = [e for e in self._compressions if not busy(e)]
        room = max(0, STORE_MAX_ENTRIES - len(alive))
        keep = {id(e) for e in alive} | {id(e) for e in (idle[-room:] if room else [])}
        self._compressions = [e for e in self._compressions if id(e) in keep]

    def remove(self, entry: dict):
        with self._lock:
            self._compressions = [e for e in self._compressions if e is not entry]

    def promote_pending(self) -> int:
        """把已就绪的 pending 压缩转正为活条目,回收其父条目(死条目),并落盘。返回转正条数。

        子压缩转正(prefix 就绪)后,它建立其上的父条目此后永不会再被选为 best → 回收掉,
        使每个会话在表里只剩 1 条活条目,避免死条目把全局名额(STORE_MAX_ENTRIES)堆满、
        把别的并发会话挤出去。先置 prefix 再删父,中间无空窗;父可能为 None(本会话首压)
        或已被删,remove 容错。

        字段转正(prefix/original_hashes/pending*)在锁内完成,与 covers/find_match 同锁互斥,
        使去重判定永远读到一致快照(不会撞见 pending 已清而 original_hashes 尚未就绪的半态);
        含 IO 的收尾(remove/persist/log)移到锁外——_lock 不可重入,remove/persist 自带锁,
        放锁内会二次获取而死锁;日志放锁外则避免持锁做盘 IO 阻塞并发热路径。"""
        promoted = 0
        reaped = []        # 待回收的父条目,锁外统一 remove
        done = []          # (prefix_len, orig_len, had_parent),锁外打印,不在锁内做日志 IO
        with self._lock:
            for entry in self._compressions:  # 已持锁,_compressions 成员不会被并发改,无需拷贝
                if entry.get("pending") is None:
                    continue
                entry["prefix"] = entry["pending"]
                entry["original_hashes"] = entry["pending_hashes"]
                entry["pending"] = None
                entry["pending_hashes"] = None
                promoted += 1
                parent = entry.pop("parent", None)
                if parent is not None and parent is not entry:
                    reaped.append(parent)
                done.append((len(entry["prefix"]), len(entry["original_hashes"]), parent is not None))
        for parent in reaped:
            self.remove(parent)  # remove 自带锁;放锁外避免对不可重入 Lock 二次获取
        for prefix_len, orig_len, had_parent in done:
            log.info(
                f"[MSG] Compression promoted: {prefix_len} prefix messages "
                f"replacing {orig_len} originals"
                f"{' (reaped 1 parent)' if had_parent else ''}"
            )
        if promoted:
            self.persist()  # 转正即落盘,重启后免冷启动满历史发送
        return promoted

    @property
    def compressions(self):
        return self._compressions


store = CompressionStore()

# 请求级统计采集器:每个真实 /v1/messages 生成调用记一条(token + 各类耗时),
# 内存环形缓冲 + JSONL 落盘,供 /stats 看板读取。
# 落盘路径走 _CLAUDE_DIR(尊重 ROLLING_CONTEXT_STATE_DIR),与 pid/version/store 一致——
# 否则隔离实例(冒烟测试、DEV 备用实例)会污染真实 ~/.claude 的统计文件。
stats = StatsCollector(path=os.path.join(_CLAUDE_DIR, "rolling-context-stats.jsonl"))


def _record_compression_call(rec: dict):
    """compressor.stats_sink 回调:摘要器自己的 HTTP 调用不经过本代理的请求路径,
    故由压缩器回灌一条记录。这里补上 ts/session(后台线程已 _set_sess 到发起会话)后入库,
    让「压缩请求」也出现在 /stats(含 200000 超限这类摘要器 400)。"""
    rec.setdefault("ts", time.time())
    rec.setdefault("session", _get_sess())
    try:
        stats.record(rec)
    except Exception as ex:
        log.debug(f"[BG] compression stat record failed: {ex}")


compressor.stats_sink = _record_compression_call


# ── 输出明细拆分 + 大请求归档 ──────────────────────────────────────────────
# proxy 透明转发、不解析 SSE,故 output_tokens 只是上游回报的总量,不知 thinking/text/tool_use 各占多少。
# 下面在响应已全量回给 CC 之后,从 buffer 副本解析出三段明细 + 可读内容块;超阈值的回合再整份归档备查。

_ARCHIVE_DIR = os.path.join(_CLAUDE_DIR, "rolling-context-archive")


def _bucket_of(block_type: str) -> str:
    """内容块 type → 三段桶。"""
    if block_type in ("thinking", "redacted_thinking"):
        return "thinking"
    if block_type in ("tool_use", "server_tool_use"):
        return "tool_use"
    return "text"


def _finalize_blocks(by_index: dict):
    """把 {index:{type,parts,name?}} 收成 (有序可读块列表, 三段字符数)。"""
    blocks = []
    bd = {"thinking": 0, "text": 0, "tool_use": 0}
    for idx in sorted(by_index):
        slot = by_index[idx]
        txt = "".join(slot.get("parts", []))
        t = slot.get("type") or "text"
        blk = {"type": t, "text": txt}
        if slot.get("name"):
            blk["name"] = slot["name"]
        blocks.append(blk)
        bd[_bucket_of(t)] += len(txt)
    return blocks, bd


def _parse_output_blocks(buffer_text: str):
    """从流式 SSE 缓冲解析出有序内容块 + 三段字符数。一趟扫描,容错跳过坏行。"""
    by_index = {}
    for line in buffer_text.split("\n"):
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            continue
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        et = data.get("type", "")
        if et == "content_block_start":
            idx = data.get("index", 0)
            cb = data.get("content_block", {}) or {}
            slot = {"type": cb.get("type", ""), "parts": []}
            if cb.get("type") in ("tool_use", "server_tool_use") and cb.get("name"):
                slot["name"] = cb.get("name")
            by_index[idx] = slot
        elif et == "content_block_delta":
            idx = data.get("index", 0)
            d = data.get("delta", {}) or {}
            dt = d.get("type", "")
            slot = by_index.setdefault(idx, {"type": "", "parts": []})
            if dt == "text_delta":
                slot["type"] = slot["type"] or "text"
                slot["parts"].append(d.get("text", ""))
            elif dt == "thinking_delta":
                slot["type"] = slot["type"] or "thinking"
                slot["parts"].append(d.get("thinking", ""))
            elif dt == "input_json_delta":
                slot["type"] = slot["type"] or "tool_use"
                slot["parts"].append(d.get("partial_json", ""))
            elif dt == "signature_delta":
                slot["type"] = slot["type"] or "thinking"  # thinking 签名,不计入可读正文
    return _finalize_blocks(by_index)


def _parse_output_blocks_json(data: dict):
    """从非流式 JSON 响应的 content 数组解析出有序内容块 + 三段字符数。"""
    by_index = {}
    for i, cb in enumerate(data.get("content", []) or []):
        t = cb.get("type", "")
        if t == "text":
            txt = cb.get("text", "")
        elif t == "thinking":
            txt = cb.get("thinking", "")
        elif t == "redacted_thinking":
            txt = cb.get("data", "")
        elif t in ("tool_use", "server_tool_use"):
            txt = json.dumps(cb.get("input", {}), ensure_ascii=False)
        else:
            txt = ""
        slot = {"type": t or "text", "parts": [txt]}
        if cb.get("name"):
            slot["name"] = cb["name"]
        by_index[i] = slot
    return _finalize_blocks(by_index)


def _archive_dir() -> str:
    try:
        os.makedirs(_ARCHIVE_DIR, exist_ok=True)
    except Exception:
        pass
    return _ARCHIVE_DIR


def _should_archive(record: dict) -> bool:
    """大输出或长耗时的真实生成回合才归档(压缩器自身调用、count 探测不归档)。"""
    if not ARCHIVE or record.get("kind") != "request":
        return False
    return ((record.get("output_tokens", 0) or 0) >= ARCHIVE_MIN_OUT
            or (record.get("t_total_ms", 0) or 0) >= ARCHIVE_MIN_MS)


def _redact(obj):
    """递归抹掉疑似密钥(归档请求体兜底脱敏;auth 本在 header 不在 body,这是双保险)。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in ("authorization", "x-api-key", "api_key", "apikey"):
                out[k] = "<redacted>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    if isinstance(obj, str) and obj.startswith("sk-") and len(obj) > 20:
        return "<redacted>"
    return obj


def _prune_archive_dir(cap_bytes: int):
    """归档目录总量超 cap 则按修改时间删最旧,直到回落到 cap 之下。"""
    try:
        entries = []
        total = 0
        for name in os.listdir(_ARCHIVE_DIR):
            if not name.endswith(".json.gz"):
                continue
            p = os.path.join(_ARCHIVE_DIR, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        if total <= cap_bytes:
            return
        entries.sort()  # 最旧的 mtime 在前
        for _mtime, size, p in entries:
            if total <= cap_bytes:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass
    except Exception as e:
        log.debug(f"[MSG] archive prune failed: {e}")


def _write_archive(record: dict, payload: dict, out_blocks: list, error_snippet=None):
    """把一份完整请求 + 响应内容落 gzip 存档,文件名记回 record['archive_file'];写完按总量封顶清理。"""
    try:
        d = _archive_dir()
        sess = record.get("session", "--------")
        out = record.get("output_tokens", 0) or 0
        tot = record.get("t_total_ms", 0) or 0
        ts = record.get("ts", time.time())
        fname = f"{ts:.0f}-{sess}-{out}tok-{tot:.0f}ms.json.gz"
        fpath = os.path.join(d, fname)
        doc = {
            "meta": {
                "ts": ts,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                "model": record.get("model"),
                "session": sess,
                "status": record.get("status"),
                "usage": {k: record.get(k, 0) for k in
                          ("input_tokens", "cache_read", "cache_create", "output_tokens")},
                "timing_ms": {k: record.get(k, 0) for k in
                              ("t_overhead_ms", "t_prefill_ms", "t_gen_ms", "t_total_ms")},
                "output_breakdown_chars": {
                    "thinking": record.get("out_thinking_chars", 0),
                    "text": record.get("out_text_chars", 0),
                    "tool_use": record.get("out_tool_chars", 0),
                },
                "flags": {k: record.get(k) for k in
                          ("injected", "prewarm", "emergency", "first_compressed", "concurrent")},
                "stream_chunks": record.get("stream_chunks", 0),
            },
            "request": _redact(payload),
            "response": {"status": record.get("status"), "blocks": out_blocks},
        }
        if error_snippet:
            doc["response"]["error"] = error_snippet
        with gzip.open(fpath, "wt", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        record["archive_file"] = fname
        _prune_archive_dir(ARCHIVE_CAP_MB * 1024 * 1024)
        log.info(f"[MSG] Archived big/slow request -> {fname}")
        return fname
    except Exception as e:
        log.debug(f"[MSG] archive write failed: {e}")
        return None

# 并发检测:保存「在途」生成请求的 record 引用。任意时刻在途 >1 个,即把彼此都标记
# concurrent —— 这样并发期内每个请求都带标记,看板可把并发吞吐与单请求吞吐分开统计。
_inflight_lock = threading.Lock()
_inflight = []


def _capture_error_source(record, resp, buffer):
    """对 >=400 的响应记录来源指纹,判定错误是 Cloudflare 边缘还是上游(sub2api)origin 生成。

    关键:sub2api 多半也挂在 Cloudflare 后面,因此 `Server: cloudflare` / `CF-RAY` 几乎所有响应
    都带,不能据此判 CF。真正的判据是「谁生成了这个错误体」:
      - Cloudflare 自身拦截:多为 text/html 错误页,或带 `cf-mitigated`;无源站 `Via` / `X-Request-Id`。
      - 上游 origin(sub2api):JSON 错误体 + 源站标记(`Via: Caddy`、`X-Request-Id` 等),CF 仅透传。
    原始指纹一并存库(err_*),前端可自行复核判定。
    """
    def h(name):
        try:
            return resp.getheader(name) or ""
        except Exception:
            return ""

    ctype = h("content-type")
    cf_ray = h("cf-ray")
    cf_mit = h("cf-mitigated")
    via = h("via")
    xreq = h("x-request-id") or h("x-client-request-id")
    snippet = ""
    try:
        snippet = buffer.decode("utf-8", "replace")[:500]
    except Exception:
        pass
    low = (ctype + " " + snippet).lower()
    is_html = "text/html" in ctype.lower()
    if cf_mit or (is_html and ("cloudflare" in low or "attention required" in low or cf_ray)):
        src = "cloudflare"
    elif "json" in ctype.lower() or via or xreq:
        src = "upstream"
    elif cf_ray:
        src = "cloudflare"
    else:
        src = "upstream"

    record["err_source"] = src
    record["err_server"] = h("server")
    record["err_cf_ray"] = cf_ray
    record["err_cf_mitigated"] = cf_mit
    record["err_via"] = via
    record["err_ctype"] = ctype
    record["err_retry_after"] = h("retry-after")
    record["err_snippet"] = snippet
    log.warning(
        f"[MSG] HTTP {resp.status} err_source={src} server={h('server')!r} "
        f"cf_ray={cf_ray!r} cf_mitigated={cf_mit!r} via={via!r} ctype={ctype!r} "
        f"retry_after={h('retry-after')!r} body={snippet!r}"
    )

# 看板 HTML 与 server.py 同目录;代理工作目录即 proxy/,但用 __file__ 定位更稳。
DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_apikey(headers: dict) -> dict:
    """config/env 显式给了 apikey 才覆盖上游鉴权；否则透传 claude 发来的 ANTHROPIC_AUTH_TOKEN（即从环境读）。"""
    if not APIKEY:
        return headers
    lowered = {k.lower(): k for k in headers}
    if "x-api-key" in lowered:
        headers.pop(lowered["x-api-key"], None)
    headers[lowered.get("authorization", "Authorization")] = f"Bearer {APIKEY}"
    return headers


def _forward_headers(req_headers: dict, body: bytes = None, strip_encoding: bool = False) -> dict:
    headers = {}
    for key, value in req_headers.items():
        lower = key.lower()
        if lower in ("host", "transfer-encoding", "connection", "content-length"):
            continue
        if strip_encoding and lower == "accept-encoding":
            continue
        headers[key] = value
    if body is not None:
        headers["content-length"] = str(len(body))
    _apply_apikey(headers)
    log.debug(f"[HDR] Forwarding headers: {list(headers.keys())}")
    return headers


def get_passthrough_headers(req_headers: dict) -> dict:
    headers = {}
    for key, value in req_headers.items():
        lower = key.lower()
        if lower not in ("host", "content-length", "transfer-encoding"):
            headers[key] = value
    return headers


def _apply_disguise(auth_headers: dict) -> dict:
    """代理自发请求(后台压缩 / emergency 兜底)用「最近大请求」的伪装头模板替换头,鉴权仍用当次请求的。
    开关关或尚无模板时原样返回(安全回退到当次透传头,不破坏压缩)。"""
    if not DISGUISE_CLIENT:
        return auth_headers
    with _disguise_lock:
        tmpl = dict(_disguise_template) if _disguise_template else None
    if not tmpl:
        return auth_headers
    out = dict(tmpl)
    for k, v in auth_headers.items():
        if k.lower() in ("authorization", "x-api-key"):
            out[k] = v
    return out


def _maybe_capture_disguise(req_headers: dict, raw_body_len: int, is_count: bool) -> None:
    """超 TARGET 的真实大请求 → 刷新伪装模板(排除鉴权/连接编码头)。注入后的小请求不超阈值,不污染模板。"""
    if not DISGUISE_CLIENT or is_count:
        return
    if _estimate_body_tokens(raw_body_len) <= TARGET_TOKENS:
        return
    tmpl = {k: v for k, v in req_headers.items() if k.lower() not in _DISGUISE_SKIP}
    global _disguise_template
    with _disguise_lock:
        _disguise_template = tmpl


def _request_window(req_headers: dict) -> int:
    """判出本请求的真实上下文窗口上限(token)。config 的 context_window 显式覆盖优先(供第三方上游
    谎报 1M 时钉死);否则读 anthropic-beta 头:含 context-1m → 1M,否则 200k。req_headers 是原始
    大小写 dict,故 .lower() 做大小写无关匹配。"""
    if CONTEXT_WINDOW_OVERRIDE > 0:
        return CONTEXT_WINDOW_OVERRIDE
    for k, v in req_headers.items():
        if k.lower() == "anthropic-beta" and _BETA_1M_MARK in (v or "").lower():
            return WINDOW_1M
    return WINDOW_DEFAULT


def _effective_trigger(req_headers: dict) -> int:
    """主动压缩的有效阈值:不超过真实窗口×安全余量,避免 trigger 配超导致永不触发、撞墙吃 400。
    正常配置(trigger 已低于该值)不受影响,只在配超时才夹紧。"""
    return min(TRIGGER_TOKENS, int(_request_window(req_headers) * TRIGGER_SAFETY))


def _hard_ceiling(eff_trigger: int, window: int) -> int:
    """饥饿逃生阀硬顶:超长工具循环中 token 超过该值时,不再等 end_turn、循环中强制建条目。
    trigger×1.2 给正常短循环留缓冲(实测单循环可涨 15k+ 后自然 end_turn);窗口×95% 封顶,
    保证 200k 窗口下先于撞墙触发(180k×1.2=216k 会越过 200k 窗口,夹回 190k)。"""
    return min(int(eff_trigger * 1.2), int(window * 0.95))


def _estimate_body_tokens(raw_body_len: int) -> int:
    """从原始请求体字节数粗估 token。整体口径(含 system+tools+messages),与 breakdown 日志同除数 4。
    偏低估(JSON 结构使字节/token 高于纯文本),故据此判「超 trigger」是保守的:只在请求确实很大时才触发。"""
    return raw_body_len // 4


def _image_excess_bytes(messages: list) -> int:
    """图片 base64 超出「单图 token 上限」的字节当量合计,用于修正 body 字节粗估。
    一张截图的 base64 可达数百 KB,按 ÷4 口径会虚增十几万 token(真实单图 ≤~1600 token),
    曾把带图透传请求的估算虚高近 3 倍、未到 trigger 也被误判超限反复 proactive(压缩风暴的放大器)。
    单图 token 与 compressor._image_chars 同口径:min(1600, max(1, b64//1000))。"""
    excess = 0
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            subs = [b] if b.get("type") == "image" else (
                b.get("content") if b.get("type") == "tool_result"
                and isinstance(b.get("content"), list) else [])
            for s in subs:
                if isinstance(s, dict) and s.get("type") == "image":
                    b64 = (s.get("source") or {}).get("data", "")
                    tokens = min(1600, max(1, len(b64) // 1000))
                    excess += max(0, len(b64) - tokens * 4)
    return excess


def _should_proactive_compress(raw_body_len: int, req_headers: dict, is_count: bool, injected: bool) -> bool:
    """是否在转发前主动同步压缩:开关开 + 非 count 探测 + 未命中缓存(否则已是小请求) + 粗估超有效 trigger。"""
    if not PROACTIVE_COMPRESS or is_count or injected:
        return False
    return _estimate_body_tokens(raw_body_len) > _effective_trigger(req_headers)


def _validate_tool_pairs(messages: list) -> list:
    tool_use_ids = set()
    valid_from = 0
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        tool_use_ids.add(block.get("id", ""))
                    elif block.get("type") == "tool_result":
                        if block.get("tool_use_id", "") not in tool_use_ids:
                            valid_from = i + 1
    if valid_from > 0:
        log.info(f"Dropping {valid_from} messages with orphaned tool_result references")
    return messages[valid_from:]


def _injection_is_safe(merged: list) -> bool:
    """注入产物结构自检:防止发给上游的消息畸形(问题②:压缩后 count / 文本化 <invoke> / 工具失败)。
    必须同时满足:
      ① 以 summary(user) 开头——_validate_tool_pairs 遇到边界孤儿 tool_result 会「从头前切」,
         可能把注入的 summary 前缀一起丢掉;摘要不在首条即前缀已被切,结构不可信。
      ② 末尾不是 assistant——末尾 assistant 会被上游当 prefill,模型续写而非新开一轮,
         原生 tool_use 通道不可用 → 工具调用退化成纯文本、开头漏出接续残片(用户看到的 count)。
         注意只拒 assistant:CC 会把任务提醒/IDE 诊断等附件作为独立的 role:"system" 消息挂在
         messages 末尾,这类请求完全合法(1.20.0 曾把尾部写死成 ==user,把它们全误判为畸形,
         配合误删条目造成「压缩风暴」)。
    任一不满足即返回 False,调用方应放弃注入、原样透传本发(交给 proactive/emergency 兜底)。"""
    if not merged or len(merged) < 2:
        return False
    if merged[0].get("role") != "user" or merged[-1].get("role") == "assistant":
        return False
    c0 = merged[0].get("content", "")
    return isinstance(c0, str) and SUMMARY_MARKER in c0


def _mark_cache_breakpoint(msg: dict) -> bool:
    """在单条消息末尾打一个 ephemeral cache_control 断点,作为增量缓存锚点。返回是否打上。

    cache_control 只能挂在 content block 上:content 为字符串时先转成单个 text block;
    为 block 列表时从末尾找第一个可缓存类型(text/tool_result/tool_use)挂上——跳过
    thinking 等不可挂断点的块,避免上游 400。
    """
    content = msg.get("content", "")
    if isinstance(content, str):
        msg["content"] = [{
            "type": "text", "text": content, "cache_control": {"type": "ephemeral"},
        }]
        return True
    if isinstance(content, list):
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") in ("text", "tool_result", "tool_use"):
                block["cache_control"] = {"type": "ephemeral"}
                return True
    return False


def _do_background_compression(entry: dict, messages: list, auth_headers: dict, real_token_count: int = None, sess: str = "--------"):
    """Compress messages. Key = hashes of messages that were summarized (not kept verbatim)."""
    auth_headers = _apply_disguise(auth_headers)  # 套「最近大请求」伪装头模板(开关关/无模板则原样)
    # 后台线程不继承请求线程的会话标签,显式重设,使 [BG]/压缩器日志归属到发起会话。
    _set_sess(sess)
    log.info(f"[BG] Starting compression of {len(messages)} messages...")
    try:
        compressed, prefix_len = compressor.compress(messages, auth_headers, real_token_count=real_token_count)
        # prefix_len==0 即直通(无可压旧消息):不要把原始消息误存为压缩条目(会生成垃圾 key 污染
        # 匹配)。直接移除本 entry 后返回。
        if prefix_len == 0:
            log.info("[BG] Pass-through (nothing to compress), discarding entry")
            entry["pending"] = None
            store.remove(entry)
            return
        # compressed = [前缀] + recent_verbatim。前缀长度由模式决定:
        #   clean 模式 prefix_len=2([summary, ack]);toolpair 模式 prefix_len=1([summary])。
        # Prefix = ONLY 摘要前缀 — 逐字保留段注入时取自原始请求,放进前缀会重复。
        prefix = compressed[:prefix_len]
        # Key = the messages that were summarized away (not the verbatim ones).
        key_hashes, summarized = _compression_key_hashes(messages, compressed, prefix_len)
        # 发布顺序:先备好 hashes/debug,最后才置 pending。promote_pending 以「pending 非空」为
        # 转正信号,故 pending 一旦可见就必须保证 pending_hashes 已就绪——否则会读到半成品、
        # 把 original_hashes 置空,这条压缩白丢还留个永不命中的死条目。
        entry["pending_hashes"] = key_hashes
        entry["intent_hashes"] = None  # pending_hashes 接棒,intent 使命完成
        entry["_debug_messages"] = summarized  # for mismatch debugging
        entry["pending"] = prefix
        log.info(
            f"[BG] Compression ready: "
            f"{compressor._count_chars(prefix):,} chars "
            f"({len(prefix)} prefix messages, key={len(key_hashes)} hashes, "
            f"summarized {len(summarized)} messages)"
        )
    except Exception as e:
        log.error(f"[BG] Compression failed: {e}", exc_info=True)
        # 压缩失败:显式移除本 entry,立即释放名额并与 prefix_len==0 直通分支保持一致。
        # 留着也无害(三段哈希链为空,covers/find_match 不会命中),但等 prune 回收会多占一个槽。
        entry["pending"] = None
        entry["intent_hashes"] = None
        store.remove(entry)


def _emergency_compress(messages: list, auth_headers: dict, reported_tokens, msg_chars: int):
    """同步压缩兜底:上游以「prompt too long」拒了超限请求后,当场把旧消息摘掉、只留近端,返回可直接
    发送的【完整新消息数组】。同时把这次压缩登记进 store(prefix + key 链),让后续请求直接命中、不必
    再付这次的同步延迟。压不出可压旧消息时返回 None(交由调用方原样回 400)。

    keep 比例的分母优先用错误体里上游自报的真实 token 数(最准,一次即压到上限内);解析不到再用
    msg_chars/3 粗估兜底。"""
    real = reported_tokens or (msg_chars // 3 if msg_chars else None)
    auth_headers = _apply_disguise(auth_headers)  # 套「最近大请求」伪装头模板(开关关/无模板则原样)
    compressed, prefix_len = compressor.compress(messages, auth_headers, real_token_count=real)
    if prefix_len == 0:
        log.warning("[EMG] Nothing to compress (prefix_len=0); cannot shrink request")
        return None, None
    key_hashes, summarized = _compression_key_hashes(messages, compressed, prefix_len)
    entry = store.add()
    entry["prefix"] = compressed[:prefix_len]
    entry["original_hashes"] = key_hashes
    entry["_debug_messages"] = summarized
    entry["used"] = True          # 已当场用上,不再标「首次注入」
    entry["pre_tokens"] = real or 0
    store.persist()  # 同步兜底产出的条目也落盘,重启后仍可命中
    log.info(
        f"[EMG] Synchronous compression done: {len(messages)} -> {len(compressed)} messages "
        f"({compressor._count_chars(compressed):,} chars, key={len(key_hashes)} hashes); "
        f"registered for future matches"
    )
    return compressed, entry  # 返回条目,供调用方在随后触发后台压缩时当父条目回收


class ProxyHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests, proxy to upstream API."""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _get_headers_dict(self) -> dict:
        return {key: value for key, value in self.headers.items()}

    def _tag_session(self):
        """从 X-Claude-Code-Session-Id 取会话标签(UUID 前 8 位),设进线程本地,供日志区分会话。"""
        sid = self.headers.get("X-Claude-Code-Session-Id", "") or ""
        _set_sess(sid[:8] if sid else "no-sess-")

    def _proxy_raw(self, method: str):
        """Raw proxy — forward request and stream response back."""
        body = self._read_body()
        headers = _forward_headers(self._get_headers_dict(), body if body else None)

        log.info(f"[RAW] {method} {self.path} -> {UPSTREAM_URL} (body={len(body)} bytes)")

        try:
            conn = _upstream_conn()
            upstream_full_path = _join_path(UPSTREAM_PATH, self.path)
            conn.request(method, upstream_full_path, body=body if body else None, headers=headers)
            resp = conn.getresponse()

            log.info(f"[RAW] Response: {resp.status} {resp.reason}")

            self.send_response(resp.status)
            resp_headers = resp.getheaders()
            log.debug(f"[RAW] Response headers: {resp_headers}")
            has_content_length = False
            for key, value in resp_headers:
                lower = key.lower()
                if lower in ("connection", "transfer-encoding"):
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()

            total_bytes = 0
            while True:
                # read1: 单次 socket 读有多少返回多少,不凑满 8KB 才返回。
                # 对 chunked SSE,每个事件到立刻 flush,流式与直连一致顺畅。
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total_bytes += len(chunk)

            log.info(f"[RAW] Done streaming {total_bytes:,} bytes")
            conn.close()
        except Exception as e:
            log.error(f"[RAW] Upstream error: {e}", exc_info=True)
            error_body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)

    def do_GET(self):
        self._tag_session()
        log.info(f"[REQ] GET {self.path}")
        parsed = urlparse(self.path)
        normalized_path = parsed.path
        if normalized_path == "/health":
            self._handle_health()
        elif normalized_path == "/debug/compressions":
            self._handle_debug_compressions()
        elif normalized_path in ("/stats", "/stats/"):
            self._handle_stats_page()
        elif normalized_path == "/stats/data":
            self._handle_stats_data(parsed)
        elif normalized_path == "/stats/archive":
            self._handle_archive_list()
        elif normalized_path == "/stats/archive/get":
            self._handle_archive_get(parsed)
        else:
            self._proxy_raw("GET")

    def do_POST(self):
        self._tag_session()
        log.info(f"[REQ] POST {self.path}")
        if self.path.startswith("/v1/messages"):
            self._handle_messages()
        else:
            self._proxy_raw("POST")

    def do_PUT(self):
        self._tag_session()
        log.info(f"[REQ] PUT {self.path}")
        self._proxy_raw("PUT")

    def do_DELETE(self):
        self._tag_session()
        log.info(f"[REQ] DELETE {self.path}")
        self._proxy_raw("DELETE")

    def do_PATCH(self):
        self._tag_session()
        log.info(f"[REQ] PATCH {self.path}")
        self._proxy_raw("PATCH")

    def do_OPTIONS(self):
        self._tag_session()
        log.info(f"[REQ] OPTIONS {self.path}")
        self._proxy_raw("OPTIONS")

    def _handle_debug_compressions(self):
        entries = []
        for i, entry in enumerate(store.compressions):
            info = {
                "index": i,
                "hash_chain_length": len(entry.get("original_hashes") or []),
                "has_prefix": entry["prefix"] is not None,
                "prefix_content": None,
            }
            if entry["prefix"]:
                for msg in entry["prefix"]:
                    content = msg.get("content", "")
                    if isinstance(content, str) and "[ROLLING_CONTEXT_SUMMARY]" in content:
                        info["prefix_content"] = content
            entries.append(info)
        body = json.dumps(entries, indent=2).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_health(self):
        active = sum(
            1 for e in store.compressions
            if e["thread"] is not None and e["thread"].is_alive()
        )
        # 在途转发数:供 hook 升级闸门「排空再杀」判定——旧代理正为某会话流式转发时先等它跑完再替换,
        # 避免硬杀切断在途 SSE(对端 RST → 旧会话看到 502)。
        with _inflight_lock:
            inflight = len(_inflight)
        data = {
            "status": "ok",
            "version": VERSION,
            "pid": os.getpid(),
            "trigger_tokens": TRIGGER_TOKENS,
            "target_tokens": TARGET_TOKENS,
            "summarizer_model": SUMMARIZER_MODEL,
            "upstream_url": UPSTREAM_URL,
            "compression_count": compressor.compression_count,
            "total_tokens_saved": compressor.total_tokens_saved,
            "stored_compressions": len(store.compressions),
            "active_compressions": active,
            "inflight": inflight,
        }
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_stats_page(self):
        """Serve the self-contained statistics dashboard (HTML)."""
        try:
            with open(DASHBOARD_HTML, "rb") as f:
                body = f.read()
            ctype = "text/html; charset=utf-8"
        except Exception as e:
            body = f"dashboard.html not found: {e}".encode()
            ctype = "text/plain; charset=utf-8"
            self.send_response(404)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_stats_data(self, parsed):
        """Return aggregated statistics as JSON for the dashboard charts."""
        qs = parse_qs(parsed.query)
        hours_raw = (qs.get("hours", ["24"])[0] or "24").lower()
        if hours_raw in ("all", "0", ""):
            hours = None
        else:
            try:
                hours = float(hours_raw)
            except ValueError:
                hours = 24.0
        extra = {
            "version": VERSION,
            "upstream_url": UPSTREAM_URL,
            "summarizer_model": SUMMARIZER_MODEL,
            "trigger_tokens": TRIGGER_TOKENS,
            "target_tokens": TARGET_TOKENS,
            "listen_port": LISTEN_PORT,
            "compression_count": compressor.compression_count,
            "total_tokens_saved": compressor.total_tokens_saved,
        }
        try:
            data = stats.aggregate(hours=hours, now=time.time(), extra=extra)
            body = json.dumps(data, ensure_ascii=False).encode()
        except Exception as e:
            log.error(f"[STATS] aggregate failed: {e}", exc_info=True)
            body = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_archive_list(self):
        """列出归档文件(名/大小/mtime),供 dashboard 的归档查看用。"""
        items = []
        try:
            for name in os.listdir(_ARCHIVE_DIR):
                if not name.endswith(".json.gz"):
                    continue
                try:
                    st = os.stat(os.path.join(_ARCHIVE_DIR, name))
                except OSError:
                    continue
                items.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
        except FileNotFoundError:
            pass
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)
            return
        items.sort(key=lambda x: x["mtime"], reverse=True)
        self._send_json({"dir": _ARCHIVE_DIR, "cap_mb": ARCHIVE_CAP_MB, "items": items})

    def _handle_archive_get(self, parsed):
        """解压并返回单个归档文件的 JSON。name 仅取 basename 防目录穿越。"""
        qs = parse_qs(parsed.query)
        name = os.path.basename((qs.get("name", [""])[0] or ""))
        if not name.endswith(".json.gz"):
            self._send_json({"error": "bad name"}, status=400)
            return
        fpath = os.path.join(_ARCHIVE_DIR, name)
        if not os.path.isfile(fpath):
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            with gzip.open(fpath, "rt", encoding="utf-8") as f:
                raw = f.read()
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)
            return
        body = raw.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_messages(self):
        # 统计计时:t_recv=请求进入代理;ts_epoch=墙钟时间戳(供看板按时间分桶)。
        # count_tokens 是 CC 频繁的「探测」调用、无生成,排除以免污染 token/延迟统计。
        t_recv = time.perf_counter()
        ts_epoch = time.time()
        is_count = "count_tokens" in self.path

        raw_body = self._read_body()
        req_headers = self._get_headers_dict()
        auth_headers = get_passthrough_headers(req_headers)
        _maybe_capture_disguise(req_headers, len(raw_body), is_count)

        log.info(f"[MSG] POST {self.path} (body={len(raw_body)} bytes)")
        log.debug(f"[MSG] Request headers: {list(req_headers.keys())}")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            log.error("[MSG] Invalid JSON in request body")
            error_body = b'{"error":"Invalid JSON"}'
            self.send_response(400)
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        messages = payload.get("messages", [])
        is_streaming = payload.get("stream", False)
        model = payload.get("model", "unknown")

        # Hash all messages for content-based matching
        msg_hashes = _hash_messages(messages)
        msg_chars = compressor._count_chars(messages)

        log.info(
            f"[MSG] model={model} stream={is_streaming} "
            f"messages={len(messages)} chars={msg_chars:,}"
        )

        # 诊断:拆解请求体各部分占比,定位「真实 token 到底在哪」(system / tools / 消息 / 图片)。
        # _count_chars 只看 messages,system 和 tools 不在其内——这一行用 JSON 字节长度近似各部分大小
        # (≈4 字节/token),据此判断压缩计量盲区是图片还是 system+tools。只读,不改行为。
        try:
            sys_bytes = len(json.dumps(payload.get("system", ""), ensure_ascii=False))
            tools_bytes = len(json.dumps(payload.get("tools", []), ensure_ascii=False))
            n_tools = len(payload.get("tools", []) or [])
            img_count = sum(
                1
                for m in messages if isinstance(m.get("content"), list)
                for b in m["content"] if isinstance(b, dict)
                for s in ([b] if b.get("type") == "image"
                          else (b.get("content", []) if b.get("type") == "tool_result"
                                and isinstance(b.get("content"), list) else []))
                if isinstance(s, dict) and s.get("type") == "image"
            )
            log.info(
                f"[MSG] breakdown: body={len(raw_body):,}B "
                f"system={sys_bytes:,}B tools={tools_bytes:,}B({n_tools} tools) "
                f"msg_count_chars={msg_chars:,} images={img_count} "
                f"(≈{len(raw_body)//4:,} tok @4B/tok)"
            )
        except Exception as e:
            log.debug(f"[MSG] breakdown failed: {e}")

        # Promote any pending compressions(转正 + 回收父死条目 + 落盘,见 promote_pending)
        store.promote_pending()

        # Scan: do any stored compressions match this request's messages?
        match, match_end = store.find_match(msg_hashes, messages)
        injected = False
        first_compressed = False   # 本请求是否「首次」用上某条新压缩(看板标记压缩生效点)
        pre_compress_tokens = 0    # 该压缩对应的压缩前 token 规模(展示收缩效果)

        if match and match["prefix"] is not None and match_end > 0:
            # Replace everything up to match_end with the prefix
            # (prefix contains summary of everything before it)
            new_messages = messages[match_end:]
            merged = match["prefix"] + new_messages
            merged = _validate_tool_pairs(merged)

            if not _injection_is_safe(merged):
                # 真畸形(summary 前缀被前切 / 末尾 assistant 会被上游当 prefill)才走到这:
                # 放弃本发注入、原样透传(injected 保持 False),交给 proactive/emergency 兜底。
                # 条目本身保留——它经内容哈希自校验,对其他结构正常的请求可能完全健康;
                # 1.20.0 曾在此 store.remove 误删健康条目,是「压缩风暴」的引擎。
                log.warning(
                    "[MSG] Injection structurally unsafe for this request "
                    f"(head={merged[0].get('role') if merged else None} "
                    f"tail={merged[-1].get('role') if merged else None} n={len(merged) if merged else 0}); "
                    "skipping injection, passing through original"
                )
                match = None
            else:
                # 注入后重建缓存断点,而非留下「零断点」。
                # 原逻辑删光 merged 里所有 cache_control,导致整个 messages 无断点 → 上游不缓存,
                # 每个注入轮按全新输入计费(只剩 system+tools 命中)。改为:先删净旧断点(避免位置失效 /
                # 超过 4 个上限),再在两个稳定边界各打一个 ephemeral:
                #   1) 摘要前缀末尾(ack,merged[1]):promote 后不变 → 缓存 system+tools+summary+ack 这一大段
                #   2) 末条消息(merged[-1]):近端尾巴在 5min 窗口内跨轮 cache_read
                # 断点总数 = system + tools + 2 ≤ 4,不触发上游 400。
                _remark_cache_breakpoints(merged)

                merged_chars = compressor._count_chars(merged)
                if merged_chars < msg_chars:
                    log.info(
                        f"[MSG] Injecting: {msg_chars:,} -> {merged_chars:,} chars "
                        f"({len(messages)} -> {len(merged)} messages, "
                        f"replaced 0-{match_end} with {len(match['prefix'])} prefix "
                        f"+ {len(new_messages)} new)"
                    )
                    payload["messages"] = merged
                    msg_chars = merged_chars
                    injected = True
                    # 该压缩条目第一次被注入 → 标记为「压缩生效的第一个请求」,并带上压缩前规模。
                    if not match.get("used"):
                        match["used"] = True
                        first_compressed = True
                        pre_compress_tokens = match.get("pre_tokens", 0)
                else:
                    log.info(
                        f"[MSG] Compression no longer helps: "
                        f"merged={merged_chars:,} >= current={msg_chars:,} chars, removing"
                    )
                    store.remove(match)
                    match = None

        # Save current state for post-response compression trigger
        current_messages = payload.get("messages", messages)

        # 主动同步压缩:未命中缓存的大请求,在转发上游【之前】就按请求体大小估算 token,超有效 trigger 就
        # 当场同步压一次、把结果换进请求体再发——而非先全量发、再后台压。专治 resume 冷启动那发:旧路径要
        # 先把全量 transcript 打给慢上游(缓存全冷,可达数十秒~数分钟,用户以为卡死),且账单按全量计;主动
        # 压后第一发即缩到 target 上下,且压缩条目当场登记进 store,后续请求直接命中、不再付这次同步延迟。
        prewarm = False
        proactive_entry = None
        # 估算前先扣图片超额字节:base64 图按 ÷4 会虚增十几万 token/张,带图透传请求会被误判超 trigger。
        body_est_len = max(0, len(raw_body) - _image_excess_bytes(current_messages))
        if _should_proactive_compress(body_est_len, req_headers, is_count, injected):
            est_tokens = _estimate_body_tokens(body_est_len)
            log.info(
                f"[MSG] Proactive compress: ~{est_tokens:,} est tokens "
                f"(body {len(raw_body):,} B, est-adjusted {body_est_len:,} B) "
                f"> trigger {_effective_trigger(req_headers):,}, no cache hit — compressing before forward"
            )
            new_msgs = None
            try:
                # reported 传 body 粗估(比 msg_chars//3 更贴近真实,含 system+tools),供压缩器定 keep 比例。
                new_msgs, proactive_entry = _emergency_compress(
                    current_messages, auth_headers, est_tokens, msg_chars
                )
            except Exception as ex:
                log.error(f"[MSG] Proactive compression failed: {ex}", exc_info=True)
            if new_msgs is not None:
                _remark_cache_breakpoints(new_msgs)
                payload["messages"] = new_msgs
                current_messages = new_msgs
                msg_chars = compressor._count_chars(new_msgs)
                injected = True
                prewarm = True

        # Forward request — strip Accept-Encoding so we get plain text SSE
        body = json.dumps(payload).encode()
        headers = _forward_headers(req_headers, body, strip_encoding=True)

        log.info(f"[MSG] Forwarding to {UPSTREAM_URL}{self.path} ({len(body):,} bytes)")

        # 本次请求的统计记录,在 try 内逐步填充(token/状态/耗时),finally 落库一次。
        record = {
            "ts": ts_epoch, "model": model, "session": _get_sess(),
            "stream": bool(is_streaming), "status": 0,
            "input_tokens": 0, "cache_read": 0, "cache_create": 0, "output_tokens": 0,
            "req_bytes": len(raw_body), "resp_bytes": 0, "injected": injected,
            "t_overhead_ms": 0, "t_prefill_ms": 0, "t_gen_ms": 0, "t_total_ms": 0,
            "concurrent": False, "stream_chunks": 0,
            "kind": "request",  # 与 compressor 回灌的 kind=="compression" 区分
            "first_compressed": first_compressed, "pre_tokens": pre_compress_tokens,
            "emergency": False,  # 是否走了「超限→同步压缩重试」兜底
            "prewarm": prewarm,  # 是否走了「转发前主动同步压缩」(resume 冷启动提速)
        }
        t_first = None
        t_fwd = t_recv
        # 输出明细 + 归档:在 try 内填充(流式解析后),finally 统一判阈值落档。预置默认值,
        # 即便上游连接异常提前抛出,finally 也能安全引用。
        out_blocks = []
        out_breakdown = {"thinking": 0, "text": 0, "tool_use": 0}
        archive_err = None

        # 注册在途(仅真实生成请求;count_tokens 探测不计入并发)。
        if not is_count:
            with _inflight_lock:
                if _inflight:
                    record["concurrent"] = True
                    for r in _inflight:
                        r["concurrent"] = True
                _inflight.append(record)

        try:
            conn = _upstream_conn()
            upstream_full_path = _join_path(UPSTREAM_PATH, self.path)
            t_fwd = time.perf_counter()  # 转发上游的时刻;首字延迟 = t_first - t_fwd
            conn.request("POST", upstream_full_path, body=body, headers=headers)
            resp = conn.getresponse()

            log.info(f"[MSG] Upstream response: {resp.status} {resp.reason}")

            # 永不超发兜底:未命中缓存时,若上游因「prompt too long」拒了这发超限请求,就当场同步压一次、
            # 把结果换进请求体重试一发——CC 端永不看到这发 400。也让 CC 自己的 autoCompact/`/compact`
            # 那发超限摘要请求得以成功,把真实 transcript 焊小。最多重试一发,压不动/仍失败则原样回 400。
            prebuffered = None  # 非重试的 400:已读出的错误体,直接回给 CC(不再走流式读取)
            emergency_entry = None  # 同步兜底登记的条目;随后若触发后台压缩,用它当父条目回收
            # prewarm 的请求虽已 injected,但主动压缩按粗估定的 keep 比例可能偏松、压后仍超限;放行让它再走
            # 一次 emergency,用 400 体里上游自报的真实 token 数重压,保住「CC 永不见 400」的兜底。命中真实
            # 压缩条目(match)而 injected 的请求不放行——那已是 known-good 尺寸,无需二次压。
            if EMERGENCY_COMPRESS and not is_count and (not injected or prewarm) and resp.status == 400:
                err_preview = resp.read()  # 400 体很小
                conn.close()
                if _looks_too_long(err_preview):
                    reported = _parse_reported_tokens(err_preview)
                    log.warning(
                        f"[MSG] Upstream 400 'too long' (reported={reported}); "
                        f"compressing synchronously and retrying once"
                    )
                    new_msgs = None
                    try:
                        new_msgs, emergency_entry = _emergency_compress(current_messages, auth_headers, reported, msg_chars)
                    except Exception as ex:
                        log.error(f"[MSG] Emergency compression failed: {ex}", exc_info=True)
                    if new_msgs is not None:
                        _remark_cache_breakpoints(new_msgs)
                        payload["messages"] = new_msgs
                        current_messages = new_msgs
                        msg_chars = compressor._count_chars(new_msgs)
                        body = json.dumps(payload).encode()
                        headers = _forward_headers(req_headers, body, strip_encoding=True)
                        record["emergency"] = True
                        record["injected"] = injected = True
                        conn = _upstream_conn()
                        t_fwd = time.perf_counter()  # 重置:压缩耗时计入 overhead,prefill 仍只量上游
                        conn.request("POST", upstream_full_path, body=body, headers=headers)
                        resp = conn.getresponse()
                        log.info(
                            f"[MSG] Emergency-compressed retry -> {resp.status} "
                            f"({len(body):,} bytes)"
                        )
                    else:
                        prebuffered = err_preview  # 压不出可压旧消息 → 原样回 400
                else:
                    prebuffered = err_preview      # 非超限类 400 → 原样回 CC

            self.send_response(resp.status)
            resp_headers = resp.getheaders()
            log.debug(f"[MSG] Response headers: {resp_headers}")
            has_content_length = False
            for key, value in resp_headers:
                lower = key.lower()
                if lower in ("connection", "transfer-encoding"):
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()

            log.info(f"[MSG] Streaming response...")

            # Stream response and capture SSE token data
            buffer = b""
            total_bytes = 0
            total_input = 0
            chunks = 0  # 收到的数据块数:真流式应有几十~上百块;个位数=上游把整条流缓冲后一次性吐出
            if prebuffered is not None:
                # 非重试的 400:错误体已整体读出(conn 已关),直接回写,不再走 read1 循环。
                self.wfile.write(prebuffered)
                self.wfile.flush()
                buffer = prebuffered
                total_bytes = len(prebuffered)
                chunks = 1
            else:
                while True:
                    # read1: 同上,逐 SSE 事件即时 flush,消除 8KB 批缓冲卡顿。
                    chunk = resp.read1(8192)
                    if not chunk:
                        break
                    if t_first is None:
                        t_first = time.perf_counter()  # 首字到达:prefill(输入处理)结束、生成开始
                    chunks += 1
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    total_bytes += len(chunk)
                    # 正常流式响应要缓存以解析 usage;错误响应(任意 stream 取值)也缓存一小段,
                    # 用于判定来源(CF 还是上游)。错误体都很小,封顶 64KB 防御异常大包。
                    if is_streaming or (resp.status >= 400 and len(buffer) < 65536):
                        buffer += chunk

            log.info(f"[MSG] Done streaming {total_bytes:,} bytes")

            # 统一解析 usage:输入(新增/缓存读/缓存创建)与输出 token。
            # input 主要来自 message_start;output 取各 message_delta 的累计最大值。
            usage_info = {"input": 0, "cache_read": 0, "cache_create": 0, "output": 0}
            # 本轮上游收尾原因(问题②根治):end_turn=一轮真正结束(干净边界,可安全建压缩条目);
            # tool_use=模型还要继续调工具、正处于 tool 循环中(此时压缩会切坏工具对,不建条目)。
            stop_reason = None
            if is_streaming and buffer:
                try:
                    text = buffer.decode("utf-8", errors="replace")
                    for line in text.split("\n"):
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            continue
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        evt_type = data.get("type", "")
                        # Anthropic 原生:usage 在 message_start.message.usage
                        if evt_type == "message_start":
                            u = data.get("message", {}).get("usage", {})
                            if u.get("input_tokens"):
                                usage_info["input"] = u.get("input_tokens", 0)
                            if u.get("cache_read_input_tokens"):
                                usage_info["cache_read"] = u.get("cache_read_input_tokens", 0)
                            if u.get("cache_creation_input_tokens"):
                                usage_info["cache_create"] = u.get("cache_creation_input_tokens", 0)
                            if u.get("output_tokens"):
                                usage_info["output"] = max(usage_info["output"], u.get("output_tokens", 0))
                        # 末尾 message_delta 携带最终 output_tokens;部分中转网关也在此给 input。
                        elif evt_type == "message_delta":
                            u = data.get("usage", {})
                            if u.get("output_tokens"):
                                usage_info["output"] = max(usage_info["output"], int(u.get("output_tokens", 0)))
                            if u.get("input_tokens") and not usage_info["input"]:
                                usage_info["input"] = int(u.get("input_tokens", 0))
                            # stop_reason 在 message_delta.delta.stop_reason
                            sr = data.get("delta", {}).get("stop_reason")
                            if sr:
                                stop_reason = sr
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse SSE for usage: {e}")
            elif not is_streaming and buffer:
                try:
                    data = json.loads(buffer)
                    u = data.get("usage", {})
                    usage_info["input"] = u.get("input_tokens", 0)
                    usage_info["cache_read"] = u.get("cache_read_input_tokens", 0)
                    usage_info["cache_create"] = u.get("cache_creation_input_tokens", 0)
                    usage_info["output"] = u.get("output_tokens", 0)
                    stop_reason = data.get("stop_reason")
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse response for usage: {e}")

            total_input = usage_info["input"] + usage_info["cache_read"] + usage_info["cache_create"]
            if total_input > 0:
                log.info(
                    f"[MSG] Usage: input={usage_info['input']:,} "
                    f"cache_read={usage_info['cache_read']:,} cache_create={usage_info['cache_create']:,} "
                    f"output={usage_info['output']:,} (billed_input={total_input:,})"
                )
            else:
                log.warning("[MSG] No usage tokens found in response")

            # 解析输出明细(thinking/text/tool_use 三段)+ 可读内容块,供 stats 透出与归档复用。
            # 只读 buffer 副本,不动透传字节流;容错包死,失败不影响请求与落库。
            try:
                if is_streaming and buffer:
                    out_blocks, out_breakdown = _parse_output_blocks(
                        buffer.decode("utf-8", errors="replace"))
                elif not is_streaming and buffer:
                    out_blocks, out_breakdown = _parse_output_blocks_json(json.loads(buffer))
            except Exception as e:
                log.debug(f"[MSG] output breakdown parse failed: {e}")
            # 错误响应:留一段错误体片段进归档(便于审 502/524/空响应到底回了啥)。
            if resp.status >= 400 and buffer:
                archive_err = buffer.decode("utf-8", errors="replace")[:2000]

            # 错误响应(>=400)记录来源指纹,判定是 Cloudflare 边缘还是上游(sub2api)origin 生成。
            if resp.status >= 400:
                _capture_error_source(record, resp, buffer)

            conn.close()

            # 计时与 usage 落入统计记录(供 finally 落库)。
            t_end = time.perf_counter()
            record["status"] = resp.status
            record["resp_bytes"] = total_bytes
            record["stream_chunks"] = chunks
            record["input_tokens"] = usage_info["input"]
            record["cache_read"] = usage_info["cache_read"]
            record["cache_create"] = usage_info["cache_create"]
            record["output_tokens"] = usage_info["output"]
            record["out_thinking_chars"] = out_breakdown.get("thinking", 0)
            record["out_text_chars"] = out_breakdown.get("text", 0)
            record["out_tool_chars"] = out_breakdown.get("tool_use", 0)
            record["t_overhead_ms"] = round((t_fwd - t_recv) * 1000, 1)
            if t_first is not None:
                record["t_prefill_ms"] = round((t_first - t_fwd) * 1000, 1)
                record["t_gen_ms"] = round((t_end - t_first) * 1000, 1)
            record["t_total_ms"] = round((t_end - t_recv) * 1000, 1)

            # Fallback: estimate tokens from chars if SSE didn't provide usage
            if total_input == 0 and msg_chars > 0:
                total_input = msg_chars // 4  # rough chars-to-tokens estimate
                log.info(
                    f"[MSG] No tokens from SSE, estimating from chars: "
                    f"{msg_chars:,} chars -> ~{total_input:,} tokens"
                )

            # Trigger compression based on token count.
            # 有效 trigger 夹到真实窗口×安全余量之下:trigger 配超真实窗口时(如为 1M 调高但实际 200k),
            # 仍能在撞墙前主动压,而非永不触发、退化到 400 emergency 兜底。
            eff_trigger = _effective_trigger(req_headers)
            # 饥饿逃生阀:end_turn 闸门优先(干净边界),但超长工具循环可能几小时不出 end_turn,
            # 条目一直不重建、token 无界上涨(实测涨到 323k,最终被 CC 自身 compact 抢先兜底、丢细节)。
            # 超过硬顶(trigger×1.2,且不越窗口 95%)时循环中也强制建条目——_select_cut 的 toolpair
            # 模式保证切点工具对完整,与 proactive/emergency 循环中压缩走的是同一套切点逻辑。
            hard_ceiling = _hard_ceiling(eff_trigger, _request_window(req_headers))
            if (
                total_input > 0 and total_input > eff_trigger
                and stop_reason != "end_turn" and total_input <= hard_ceiling
            ):
                # 问题②根治:后台建压缩条目只在「一轮真正结束」(stop_reason==end_turn)时进行——此刻对话
                # 处于干净 user 边界,_select_cut 能切在干净点、保留段不含孤儿 tool_result。tool 循环中
                # (tool_use / max_tokens 等)一律不建条目,避免切坏工具对;真涨大撞墙由转发前 proactive
                # 与 400 后 emergency 兜底同步压(那两条不受此限)。
                log.info(
                    f"[MSG] {total_input:,} tokens > trigger {eff_trigger:,} but "
                    f"stop_reason={stop_reason!r} (not end_turn) — mid tool-loop, deferring "
                    f"compression (hard ceiling {hard_ceiling:,})"
                )
            elif total_input > 0 and total_input > eff_trigger:
                if stop_reason != "end_turn":
                    log.info(
                        f"[MSG] {total_input:,} tokens > hard ceiling {hard_ceiling:,} — "
                        f"mid tool-loop starvation, compressing anyway (toolpair cut)"
                    )
                # 去重 + 登记意图原子完成:claim_compression 在一把锁内查是否已被覆盖,没有才建条目、
                # 登记 intent_hashes 并返回它;已覆盖则返回 None。in_flight_only 按「本发是否注入了压缩」
                # 决定去重口径(见 claim_compression docstring):
                # - 本发已注入(injected_via 非空):find_match 已注入最深覆盖条目却仍 > trigger → 真需
                #   更深一层,去重【不】看已转正 original_hashes,只挡在途(intent/pending)。否则会话被
                #   自身旧压缩挡住、深压缩永不建立,一路涨到 CC autocompact 兜底(1.19.x 回归)。
                # - 本发未注入:在途期间可能落地覆盖本段的压缩(下一发 find_match 会用),已转正条目也算
                #   覆盖 → 跳过,避免近乎重复。这把「检查→登记」收进锁内,取代原全局 already_compressing。
                injected_via = match if match is not None else (emergency_entry or proactive_entry)
                entry = store.claim_compression(
                    msg_hashes, exclude=injected_via, in_flight_only=injected_via is not None
                )
                if entry is None:
                    log.info(
                        f"[MSG] API reported {total_input:,} tokens (> trigger {eff_trigger:,}), "
                        f"but a matching compression already exists — skipping redundant compression"
                    )
                else:
                    win = _request_window(req_headers)
                    capped = " capped" if eff_trigger < TRIGGER_TOKENS else ""
                    log.info(
                        f"[MSG] API reported {total_input:,} tokens "
                        f"(trigger: {eff_trigger:,}{capped}, window {win:,}). "
                        f"Compressing in background..."
                    )
                    entry["pre_tokens"] = total_input
                    entry["parent"] = injected_via  # 注入所用条目即新压缩的父,转正时回收
                    t = threading.Thread(
                        target=_do_background_compression,
                        args=(entry, current_messages, auth_headers),
                        kwargs={"real_token_count": total_input, "sess": _get_sess()},
                        daemon=True,
                    )
                    t.start()
                    entry["thread"] = t

        except Exception as e:
            log.error(f"[MSG] Upstream error: {e}", exc_info=True)
            if record["status"] == 0:
                record["status"] = 502
            record["t_total_ms"] = round((time.perf_counter() - t_recv) * 1000, 1)
            if archive_err is None:
                archive_err = f"upstream connection error: {e}"
            error_body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
        finally:
            # 退出在途登记(并发检测),并落一条统计。count_tokens 探测两者都跳过。
            if not is_count:
                with _inflight_lock:
                    try:
                        _inflight.remove(record)
                    except ValueError:
                        pass
                # 大输出/长耗时回合整份归档备查(响应已回完,不增可感延迟;失败绝不影响落库)。
                try:
                    if _should_archive(record):
                        _write_archive(record, payload, out_blocks, error_snippet=archive_err)
                except Exception as ex:
                    log.debug(f"[MSG] archive failed: {ex}")
                try:
                    stats.record(record)
                except Exception as ex:
                    log.debug(f"[MSG] stats.record failed: {ex}")


class ThreadedHTTPServer(HTTPServer):
    """Handle each request in a new thread."""
    # 绑定即锁:关掉 SO_REUSEADDR,让第二个实例 bind 必然 EADDRINUSE 失败(Windows 默认开
    # REUSEADDR 会允许两个进程抢占同端口 → 双实例)。配合 main() 捕获后 exit(0),并发启动里
    # 抢锁失败者干净退出,根治原先「杀端口 + 猜 PID」那套 TOCTOU。
    # (Linux 重负载下杀旧起新偶遇残留 TIME_WAIT 连接可能短暂 EADDRINUSE,会自愈;Windows 监听端口
    #  在进程退出即释放,无此问题——而本插件主力在 Windows。)
    allow_reuse_address = False

    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    log.info(f"Starting Rolling Context Proxy v{VERSION} on {LISTEN_HOST}:{LISTEN_PORT}")
    log.info(f"  Trigger at: {TRIGGER_TOKENS:,} tokens")
    if CONTEXT_WINDOW_OVERRIDE > 0:
        log.info(f"  Context window: {CONTEXT_WINDOW_OVERRIDE:,} tokens (pinned via config; overrides header detection)")
    else:
        log.info(f"  Context window: auto-detect per request (anthropic-beta context-1m → 1M, else 200k)")
    log.info(f"  Effective trigger capped at {int(TRIGGER_SAFETY*100)}% of detected window")
    log.info(f"  Compress down to: {TARGET_TOKENS:,} tokens (recent context)")
    log.info(f"  Summarizer model: {SUMMARIZER_MODEL}")
    log.info(f"  Forwarding to: {UPSTREAM_URL}")
    log.info(f"  Matching: content-based (no sessions/fingerprints)")
    log.info(f"  Disguise client: {'on (mimic latest large request headers)' if DISGUISE_CLIENT else 'off (passthrough current request headers)'}")

    # 绑定即锁:端口被占 = 已有实例在跑,干净退出(exit 0),绝不抢占成双实例。
    try:
        server = ThreadedHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    except OSError as e:
        log.warning(f"Port {LISTEN_PORT} already in use ({e}); another instance owns it — exiting cleanly.")
        sys.exit(0)

    # 绑定成功 = 本进程是唯一实例,自报权威 PID/版本(取代 hook 猜 PID:包装层/重定向会让 hook 记错
    # PID,而进程自己写的永远准)。hook 与 refresh 据此判活、判版本。
    try:
        os.makedirs(_CLAUDE_DIR, exist_ok=True)
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        with open(VER_FILE, "w", encoding="utf-8") as f:
            f.write(VERSION)
    except Exception as e:
        log.warning(f"Could not write pid/version file: {e}")
    log.info(f"  PID: {os.getpid()}  (pidfile: {PID_FILE})")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
