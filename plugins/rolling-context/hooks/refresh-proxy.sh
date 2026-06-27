#!/usr/bin/env bash
# 一键刷新全局网关(Linux/macOS/Git-Bash)。
#
# 这是「更新网关」的唯一动作:拉取 marketplace clone 的最新代码,然后重启那个全局 5588 网关。
#   - 不需要 /plugin update(网关从 clone 跑,不从 cache 版本目录跑)
#   - 不需要重启 Claude Code(网关是独立进程,重启它即生效)
#   - 所有 Claude Code 客户端共用同一个 5588 网关,刷新一次,全体立即用上新代码
#
# 用法:在 Claude Code 里输入   ! bash ~/.claude/plugins/cache/konijiwa-plugin/rolling-context/<版本>/hooks/refresh-proxy.sh
# 或直接在终端跑这个脚本。
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
PIDFILE="$CLAUDE_DIR/rolling-context-proxy.pid"
VERFILE="$CLAUDE_DIR/rolling-context-proxy.version"
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

# 解析 clone 仓库根(marketplace git 仓库)。$SCRIPT_DIR 可能在 cache 下,也可能就在 clone 下。
CLONE_ROOT=""
case "$SCRIPT_DIR" in
  */cache/*)
    _PR="$(cd "$SCRIPT_DIR/../../../../.." 2>/dev/null && pwd)"
    _MP="$(basename "$(cd "$SCRIPT_DIR/../../.." 2>/dev/null && pwd)")"
    CLONE_ROOT="$_PR/marketplaces/$_MP" ;;
  */marketplaces/*)
    CLONE_ROOT="$(cd "$SCRIPT_DIR/../../.." 2>/dev/null && pwd)" ;;
esac

# 1) 拉最新(尽力而为;--ff-only 在分叉时失败但不破坏 clone)。
if [ -n "$CLONE_ROOT" ] && [ -d "$CLONE_ROOT/.git" ]; then
    echo "[refresh] git pull --ff-only  ($CLONE_ROOT)"
    git -C "$CLONE_ROOT" pull --ff-only || echo "[refresh] pull 跳过/失败,用现有 clone 代码重启"
else
    echo "[refresh] 未发现 clone 仓库,直接用现有代码重启"
fi

# 2) 按端口杀掉在跑的网关 —— 比读 pidfile 可靠(绕开 git-bash 下 \$! 拿到包装层 PID 的老问题)。
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
rm -f "$PIDFILE" "$VERFILE"
sleep 1

# 3) 用标准启动器从 clone 重新拉起(start-proxy 会自动解析 clone 源)。
echo "[refresh] 重新启动网关..."
bash "$SCRIPT_DIR/start-proxy.sh" >/dev/null 2>&1

# 4) 自愈 pidfile:写入真正监听该端口的 PID。
sleep 2
REAL_PID="$(_listeners | head -1)"
if [ -n "$REAL_PID" ]; then
    echo "$REAL_PID" > "$PIDFILE"
    echo "[refresh] ✅ 网关已就绪,监听 $PORT,PID $REAL_PID(版本 $(cat "$VERFILE" 2>/dev/null))"
else
    echo "[refresh] ⚠ 未检测到 $PORT 监听,请查看 $CLAUDE_DIR/rolling-context-proxy.log"
fi
