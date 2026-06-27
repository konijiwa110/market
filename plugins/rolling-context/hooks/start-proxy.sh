#!/usr/bin/env bash
# Ensure rolling context proxy is running
# Pure stdlib — no venv needed, just python

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 网关代码源:优先用 marketplace clone(单一最新源——`/plugin marketplace update` 或 `git pull`
# 刷新它即可,更新网关无需 /plugin update 重新 cache、无需重启 CC)。找不到 clone 时回退到本地
# cache 副本(可移植)。$SCRIPT_DIR 形如 <plugins>/cache/<MP>/rolling-context/<VER>/hooks。
PROXY_DIR="$SCRIPT_DIR/../proxy"
SRC_PLUGIN_JSON="$SCRIPT_DIR/../.claude-plugin/plugin.json"
case "$SCRIPT_DIR" in
  */cache/*)
    _PLUGINS_ROOT="$(cd "$SCRIPT_DIR/../../../../.." 2>/dev/null && pwd)"
    _MP_NAME="$(basename "$(cd "$SCRIPT_DIR/../../.." 2>/dev/null && pwd)")"
    _CLONE="$_PLUGINS_ROOT/marketplaces/$_MP_NAME/plugins/rolling-context"
    if [ -f "$_CLONE/proxy/server.py" ]; then
        PROXY_DIR="$_CLONE/proxy"
        SRC_PLUGIN_JSON="$_CLONE/.claude-plugin/plugin.json"
    fi
    ;;
esac
PIDFILE="$HOME/.claude/rolling-context-proxy.pid"
VERFILE="$HOME/.claude/rolling-context-proxy.version"
HOOKLOG="$HOME/.claude/rolling-context-hook.log"
CONFIG_FILE="$HOME/.claude/rolling-context.json"
# Detect Windows (git bash) — 必须在选 python 之前:Windows 的 python3 常是
# 微软商店空壳(command -v 命中但无输出),会让端口探测拿到空串。
if [[ "$(uname -s)" == MINGW* ]] || [[ "$(uname -s)" == MSYS* ]]; then
    IS_WINDOWS=true
else
    IS_WINDOWS=false
fi
if [ "$IS_WINDOWS" = true ]; then
    _py() { python "$@"; }
elif command -v python3 &>/dev/null; then
    _py() { python3 "$@"; }
else
    _py() { python "$@"; }
fi
# 端口取自 rolling-context.json > 环境变量 > 5588。
PORT=$(_py - "$CONFIG_FILE" <<'PYEOF'
import json, sys, os
try:
    c = json.load(open(sys.argv[1], encoding="utf-8")); c = c if isinstance(c, dict) else {}
except Exception:
    c = {}
print(c.get("port") or os.environ.get("ROLLING_CONTEXT_PORT") or 5588)
PYEOF
)
# 兜底:任何原因导致 PORT 为空(python 缺失/异常)都不能让 URL 丢端口。
[ -z "$PORT" ] && PORT=5588
PROXY_URL="http://127.0.0.1:$PORT"
CURRENT_VERSION=$(cat "$SRC_PLUGIN_JSON" 2>/dev/null | grep '"version"' | head -1 | sed 's/.*"version".*"\(.*\)".*/\1/')

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$HOOKLOG"
}

log "Hook started. PROXY_DIR=$PROXY_DIR IS_WINDOWS=$IS_WINDOWS"

# Always update settings.json first (even if proxy is already running)
SETTINGS_FILE="$HOME/.claude/settings.json"
update_settings() {
    local py_cmd=""
    if [ "$IS_WINDOWS" = true ]; then
        py_cmd="python"
    elif command -v python3 &>/dev/null; then
        py_cmd="python3"
    else
        py_cmd="python"
    fi

    $py_cmd - "$SETTINGS_FILE" "$PROXY_URL" <<'PYEOF'
import json, sys, os

settings_file = sys.argv[1]
proxy_url = sys.argv[2]

def _load(p):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

settings = _load(settings_file)
cfg = _load(os.path.join(os.path.expanduser("~"), ".claude", "rolling-context.json"))

if "env" not in settings or not isinstance(settings["env"], dict):
    settings["env"] = {}
env = settings["env"]

if cfg.get("upstream"):
    # 权威：上游来自 rolling-context.json（server.py 直接读），这里只强制把 claude 指向代理。
    env["ANTHROPIC_BASE_URL"] = proxy_url
    print("authoritative")
else:
    existing = env.get("ANTHROPIC_BASE_URL", "")
    if not existing:
        env["ANTHROPIC_BASE_URL"] = proxy_url
        print("set")
    elif "127.0.0.1" not in existing:
        env["ROLLING_CONTEXT_UPSTREAM"] = existing
        env["ANTHROPIC_BASE_URL"] = proxy_url
        print("chained")
    else:
        print("already")
    defaults = {
        "ROLLING_CONTEXT_PORT": "5588",
        "ROLLING_CONTEXT_TRIGGER": "160000",
        "ROLLING_CONTEXT_TARGET": "40000",
        "ROLLING_CONTEXT_MODEL": "claude-haiku-4-5-20251001",
    }
    for key, value in defaults.items():
        if key not in env:
            env[key] = value

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF
}

RESULT=$(update_settings 2>/dev/null)
case "$RESULT" in
    authoritative) log "Authoritative: ANTHROPIC_BASE_URL=$PROXY_URL (config upstream)" ;;
    set)           log "Set ANTHROPIC_BASE_URL=$PROXY_URL (settings.json)" ;;
    chained)       log "Chaining upstream (settings.json)" ;;
    already)       log "ANTHROPIC_BASE_URL already set (settings.json)" ;;
    *)             log "WARNING: Could not update settings.json" ;;
esac

# Check if proxy is already running
_kill_pid() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue" 2>/dev/null
    else
        kill "$pid" 2>/dev/null
        sleep 1
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    fi
}

_pid_alive() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "if (Get-Process -Id $pid -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" 2>/dev/null
    else
        kill -0 "$pid" 2>/dev/null
    fi
}

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if _pid_alive "$PID"; then
        # 版本闸门:同版本复用;在跑的版本 >= 本会话版本则复用(绝不降级);
        # 仅当本会话版本严格更高才重启升级。根治多版本会话把 5588 共享代理来回拽、
        # 每次重启掐断在传请求 + 清空压缩状态的「版本互踢」。
        RUNNING_VERSION=$(cat "$VERFILE" 2>/dev/null)
        if [ "$CURRENT_VERSION" = "$RUNNING_VERSION" ]; then
            log "Proxy already running (PID $PID, v$RUNNING_VERSION)"
            exit 0
        fi
        # sort -V 版本排序:取两者较大者。RUNNING 较大(>=)=> 复用,不降级。
        HIGHER=$(printf '%s\n%s\n' "$CURRENT_VERSION" "$RUNNING_VERSION" | sort -V | tail -1)
        if [ -n "$RUNNING_VERSION" ] && [ "$HIGHER" = "$RUNNING_VERSION" ]; then
            log "Proxy running newer/equal (PID $PID, v$RUNNING_VERSION >= v$CURRENT_VERSION) - reusing, no downgrade"
            exit 0
        fi
        log "Upgrading proxy ($RUNNING_VERSION -> $CURRENT_VERSION), restarting proxy (PID $PID)"
        _kill_pid "$PID"
    fi
    rm -f "$PIDFILE" "$VERFILE"
fi

# Start proxy directly — no venv needed (pure stdlib)
log "Starting proxy..."
(
    cd "$PROXY_DIR" || { log "ERROR: cannot cd to $PROXY_DIR"; exit 1; }
    PYTHON_CMD=""
    if [ "$IS_WINDOWS" = true ]; then
        PYTHON_CMD="python"
    elif command -v python3 &>/dev/null; then
        PYTHON_CMD="python3"
    else
        PYTHON_CMD="python"
    fi
    nohup $PYTHON_CMD server.py > "$HOME/.claude/rolling-context-proxy.log" 2>&1 &
    echo $! > "$PIDFILE"
    echo "$CURRENT_VERSION" > "$VERFILE"
    log "Proxy started with PID $! (v$CURRENT_VERSION)"
) &

exit 0
