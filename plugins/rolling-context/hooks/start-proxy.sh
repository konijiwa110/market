#!/usr/bin/env bash
# Ensure rolling context proxy is running
# Pure stdlib — no venv needed, just python.
#
# 生命周期模型(对齐上游原版,并修掉 fail-closed):
#   • 代码默认从本地 cache 副本跑:<...>/rolling-context/<VER>/proxy。CC 通过 `/plugin update`
#     拉新 cache、起新会话即生效 —— CC 掌舵生命周期,hook 只负责「该起就起」。
#   • 作者本地提速:设 ROLLING_CONTEXT_DEV=<仓库根> 则改从仓库跑(免 /plugin update),
#     仅本机本人有用,绝不写进任何全局清单、不污染生产生命周期。
#   • fail-open:仅当代理 /health 真活着才把 ANTHROPIC_BASE_URL 指向它;它起不来就放行到真上游,
#     绝不因代理挂掉而连累整个 Claude Code。

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 1) 选代码源:默认 cache 副本;ROLLING_CONTEXT_DEV 指向仓库时改用仓库(作者本地提速)。
PROXY_DIR="$SCRIPT_DIR/../proxy"
if [ -n "$ROLLING_CONTEXT_DEV" ] && [ -f "$ROLLING_CONTEXT_DEV/proxy/server.py" ]; then
    PROXY_DIR="$ROLLING_CONTEXT_DEV/proxy"
fi
PROXY_DIR="$(cd "$PROXY_DIR" 2>/dev/null && pwd)"
SRC_PLUGIN_JSON="$PROXY_DIR/../.claude-plugin/plugin.json"

PIDFILE="$HOME/.claude/rolling-context-proxy.pid"
HOOKLOG="$HOME/.claude/rolling-context-hook.log"
SETTINGS_FILE="$HOME/.claude/settings.json"
CONFIG_FILE="$HOME/.claude/rolling-context.json"

# Detect Windows (git bash) — 必须在选 python 之前:Windows 的 python3 常是
# 微软商店空壳(command -v 命中但无输出),会让探测拿到空串。
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
[ -z "$PORT" ] && PORT=5588
PROXY_URL="http://127.0.0.1:$PORT"
CURRENT_VERSION=$(cat "$SRC_PLUGIN_JSON" 2>/dev/null | grep '"version"' | head -1 | sed 's/.*"version".*"\(.*\)".*/\1/')

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$HOOKLOG"
}

# 判活以「活着的 /health」为唯一权威(自报 version + pid),pidfile 仅兜底。
# 健康 → 打印 "<version>\t<pid>";不健康 → 空输出。
proxy_health() {
    _py - "$PROXY_URL" <<'PYEOF'
import sys, json, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + "/health", timeout=2) as r:
        d = json.load(r)
    print("%s\t%s" % (d.get("version", ""), d.get("pid", "")))
except Exception:
    pass
PYEOF
}

_kill_pid() {
    local pid="$1"
    [ -z "$pid" ] && return 0
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
    [ -z "$pid" ] && return 1
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "if (Get-Process -Id $pid -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" 2>/dev/null
    else
        kill -0 "$pid" 2>/dev/null
    fi
}

# 腾位:杀掉占着本端口的代理(先按 /health 自报 pid,再兜底杀端口上任何监听者)。
# 仅在「确证错版本代理占着端口」时作最后手段调用——稳态复用走不到这一步,不会误杀健康实例。
_free_port() {
    _kill_pid "$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "Get-NetTCPConnection -State Listen -LocalPort $PORT -ErrorAction SilentlyContinue | ForEach-Object { if (\$_.OwningProcess -and \$_.OwningProcess -ne 0) { Stop-Process -Id \$_.OwningProcess -Force -ErrorAction SilentlyContinue } }" 2>/dev/null
    elif command -v lsof &>/dev/null; then
        lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | while read -r p; do kill -9 "$p" 2>/dev/null; done
    elif command -v fuser &>/dev/null; then
        fuser -k "$PORT/tcp" 2>/dev/null
    fi
    sleep 0.5
}

log "Hook started. PROXY_DIR=$PROXY_DIR v$CURRENT_VERSION IS_WINDOWS=$IS_WINDOWS$([ -n "$ROLLING_CONTEXT_DEV" ] && echo ' [DEV]')"

# 2) 判活 + 升级闸门:
#    • 在跑版本「可识别且 >= 本会话版本」→ 复用(绝不降级互踢)
#    • 在跑版本更低,或无法识别(如旧代理 /health 不报 version)→ 重启,让新版/新代码顶上
PROXY_HEALTHY=false
HEALTH=$(proxy_health)
if [ -n "$HEALTH" ]; then
    RUNNING_VERSION=$(printf '%s' "$HEALTH" | cut -f1)
    RUNNING_PID=$(printf '%s' "$HEALTH" | cut -f2)
    REUSE=false
    if [ -n "$RUNNING_VERSION" ] && [ "$RUNNING_VERSION" != "unknown" ] && \
       [ -n "$CURRENT_VERSION" ] && [ "$CURRENT_VERSION" != "unknown" ]; then
        HIGHER=$(printf '%s\n%s\n' "$CURRENT_VERSION" "$RUNNING_VERSION" | sort -V | tail -1)
        [ "$HIGHER" = "$RUNNING_VERSION" ] && REUSE=true
    fi
    if [ "$REUSE" = true ]; then
        log "Proxy healthy (PID $RUNNING_PID, v$RUNNING_VERSION >= v$CURRENT_VERSION) - reusing"
        PROXY_HEALTHY=true
    else
        log "Restarting proxy (running v${RUNNING_VERSION:-?} -> v$CURRENT_VERSION; stopping PID $RUNNING_PID)"
        _kill_pid "$RUNNING_PID"
        sleep 1
    fi
fi

# 3) 不健康(或刚为升级停掉)→ 启动一个新代理,轮询等就绪。
if [ "$PROXY_HEALTHY" != true ]; then
    # 兜底:pidfile 记的进程还活着但 /health 不通(卡死)→ 杀掉再起。
    if [ -f "$PIDFILE" ]; then
        PID=$(head -1 "$PIDFILE" 2>/dev/null)
        if [ -n "$PID" ] && _pid_alive "$PID"; then
            log "Stopping leftover proxy (PID $PID) before relaunch"
            _kill_pid "$PID"
            sleep 1
        fi
    fi
    _spawn_proxy() {
        (
            cd "$PROXY_DIR" || { log "ERROR: cannot cd to $PROXY_DIR"; exit 1; }
            if [ "$IS_WINDOWS" = true ]; then
                PYTHON_CMD="python"
            elif command -v python3 &>/dev/null; then
                PYTHON_CMD="python3"
            else
                PYTHON_CMD="python"
            fi
            nohup $PYTHON_CMD server.py > "$HOME/.claude/rolling-context-proxy.log" 2>&1 &
        ) &
    }
    # server.py 绑定成功后自写 pidfile/version 并答 /health。绑定即锁:并发第二个会 EADDRINUSE 干净退。
    # 轮询要确认「起来的确实是本版本」:旧/异版本代理赖着端口时 /health 会答别的 version → 仅凭此证据
    # 作最后手段杀端口腾位、再起一发(最多两发,腾不动则 fail-open)。根治「见任一健康代理就谎报已升级」。
    for attempt in 1 2; do
        log "Starting proxy from $PROXY_DIR ... (attempt $attempt)"
        _spawn_proxy
        GOT_SQUATTER=false
        SQUATTER_VER=""
        SQUATTER_PID=""
        for i in 1 2 3 4 5 6 7 8 9 10; do
            sleep 0.5
            H=$(proxy_health)
            [ -z "$H" ] && continue
            HV=$(printf '%s' "$H" | cut -f1)
            if [ "$HV" = "$CURRENT_VERSION" ]; then
                PROXY_HEALTHY=true
                break
            fi
            GOT_SQUATTER=true
            SQUATTER_VER="$HV"
            SQUATTER_PID=$(printf '%s' "$H" | cut -f2)
            break
        done
        [ "$PROXY_HEALTHY" = true ] && break
        if [ "$GOT_SQUATTER" = true ]; then
            log "Port $PORT held by v${SQUATTER_VER:-?} (PID ${SQUATTER_PID:-?}) != v$CURRENT_VERSION - freeing, retrying"
            _free_port "$SQUATTER_PID"
        else
            break
        fi
    done
    if [ "$PROXY_HEALTHY" = true ]; then
        log "Proxy is up (v$CURRENT_VERSION)"
    else
        log "WARNING: proxy did not become healthy in time - failing open to upstream"
    fi
fi

# 4) 写 settings.json:健康才指向代理;否则 fail-open 放行到真上游(绝不连累 CC)。
update_settings() {
    _py - "$SETTINGS_FILE" "$PROXY_URL" "$1" <<'PYEOF'
import json, sys, os

settings_file, proxy_url, healthy = sys.argv[1], sys.argv[2], (sys.argv[3] == "true")

def _load(p):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

settings = _load(settings_file)
cfg = _load(os.path.join(os.path.expanduser("~"), ".claude", "rolling-context.json"))
if not isinstance(settings.get("env"), dict):
    settings["env"] = {}
env = settings["env"]
has_cfg_upstream = bool(cfg.get("upstream"))

# 「指向本代理」按端口级判断(127.0.0.1:PORT),与 start-proxy.ps1 口径一致——
# 否则本机另跑别的端口的 127.0.0.1 上游时,会被误当本代理而漏掉链式/fail-open。
local_marker = proxy_url.split("//", 1)[-1]  # 127.0.0.1:PORT

if healthy:
    if has_cfg_upstream:
        # 权威:上游来自 config(server.py 直读),这里只把 claude 指向本地代理。
        env["ANTHROPIC_BASE_URL"] = proxy_url
        print("ok: BASE_URL=%s upstream=%s (config)" % (proxy_url, cfg["upstream"]))
    else:
        existing = env.get("ANTHROPIC_BASE_URL", "")
        if existing and local_marker not in existing:
            env["ROLLING_CONTEXT_UPSTREAM"] = existing
            print("ok: chaining upstream=%s" % existing)
        else:
            print("ok: BASE_URL=%s" % proxy_url)
        env["ANTHROPIC_BASE_URL"] = proxy_url
        for k, v in {
            "ROLLING_CONTEXT_PORT": "5588",
            "ROLLING_CONTEXT_TRIGGER": "160000",
            "ROLLING_CONTEXT_TARGET": "40000",
            "ROLLING_CONTEXT_MODEL": "claude-haiku-4-5-20251001",
        }.items():
            env.setdefault(k, v)
else:
    # FAIL-OPEN:代理没起来 → BASE_URL 指回真上游(或移除回落官方 API),让 CC 照常工作。
    fail_target = ""
    if has_cfg_upstream:
        fail_target = cfg["upstream"]
    elif env.get("ROLLING_CONTEXT_UPSTREAM"):
        fail_target = env["ROLLING_CONTEXT_UPSTREAM"]
    existing = env.get("ANTHROPIC_BASE_URL", "")
    if fail_target:
        env["ANTHROPIC_BASE_URL"] = fail_target
        print("failopen: BASE_URL=%s (proxy down)" % fail_target)
    elif existing and local_marker in existing:
        env.pop("ANTHROPIC_BASE_URL", None)
        print("failopen: removed BASE_URL -> default API (proxy down)")
    else:
        print("failopen: left BASE_URL as-is (proxy down)")

with open(settings_file, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF
}

RESULT=$(update_settings "$PROXY_HEALTHY" 2>/dev/null)
log "settings.json: ${RESULT:-WARNING could not update}"

exit 0
