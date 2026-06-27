#!/usr/bin/env bash
# Restart the local rolling-context gateway (Linux/macOS/Git-Bash) — dev convenience.
#
# 用途:就地重启本机网关(改完 server.py 想立刻生效,不必等下个会话的 SessionStart)。
# 它只做两件事:① 杀掉占用端口的监听进程 ② 重跑 start-proxy.sh(自动尊重 ROLLING_CONTEXT_DEV)。
# 代码更新走正常渠道:生产用 `/plugin update`;开发设 ROLLING_CONTEXT_DEV=<仓库根> 直接跑仓库。
#
# 用法:在 Claude Code 里输入   ! bash <...>/hooks/refresh-proxy.sh   或直接在终端跑。
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
CONFIG_FILE="$CLAUDE_DIR/rolling-context.json"

IS_WINDOWS=false
case "$(uname -s)" in MINGW*|MSYS*) IS_WINDOWS=true;; esac
if [ "$IS_WINDOWS" = true ]; then _py() { python "$@"; }
elif command -v python3 &>/dev/null; then _py() { python3 "$@"; }
else _py() { python "$@"; }; fi

# 端口:rolling-context.json > 环境变量 > 5588(与 start-proxy 一致)。
PORT=$(_py - "$CONFIG_FILE" <<'PYEOF' 2>/dev/null
import json, sys, os
try:
    c = json.load(open(sys.argv[1], encoding="utf-8")); c = c if isinstance(c, dict) else {}
except Exception:
    c = {}
print(c.get("port") or os.environ.get("ROLLING_CONTEXT_PORT") or 5588)
PYEOF
)
[ -z "$PORT" ] && PORT=5588

# 1) 按端口杀掉在跑的网关 —— 比读 pidfile 可靠(绕开 git-bash 下 $! 拿到包装层 PID 的老问题)。
_listeners() {
    if [ "$IS_WINDOWS" = true ]; then
        netstat -ano 2>/dev/null | grep "127.0.0.1:$PORT " | grep -i listening | awk '{print $NF}' | sort -u
    else
        lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u
    fi
}
for p in $(_listeners); do
    echo "[refresh] kill listener PID $p"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "Stop-Process -Id $p -Force -ErrorAction SilentlyContinue" 2>/dev/null
    else
        kill "$p" 2>/dev/null; sleep 1; kill -9 "$p" 2>/dev/null
    fi
done
sleep 1

# 2) 重跑标准启动器:从 cache(或 ROLLING_CONTEXT_DEV 指定的仓库)起新代理,
#    由 server.py 自写权威 pidfile/version、自答 /health。
DEV=""; [ -n "${ROLLING_CONTEXT_DEV:-}" ] && DEV=" [DEV]"
echo "[refresh] 重新启动网关 via start-proxy.sh$DEV ..."
bash "$SCRIPT_DIR/start-proxy.sh" >/dev/null 2>&1

# 3) 报告:问 /health 确认起来了。
sleep 1
HEALTH=$(_py - "http://127.0.0.1:$PORT" <<'PYEOF' 2>/dev/null
import sys, json, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + "/health", timeout=3) as r:
        d = json.load(r)
    print("v%s PID %s" % (d.get("version", "?"), d.get("pid", "?")))
except Exception:
    pass
PYEOF
)
if [ -n "$HEALTH" ]; then
    echo "[refresh] OK   网关已就绪,监听 $PORT ($HEALTH)"
else
    echo "[refresh] WARN 未检测到 $PORT 健康网关,请查看 $CLAUDE_DIR/rolling-context-proxy.log"
fi
