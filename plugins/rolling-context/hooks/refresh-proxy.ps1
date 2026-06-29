# Restart the local rolling-context gateway (Windows) — dev convenience.
#
# 用途:就地重启本机网关(改完 server.py 想立刻生效,不必等下个会话的 SessionStart)。
# 它只做两件事:① 杀掉占用端口的监听进程 ② 重跑 start-proxy.ps1(自动尊重 ROLLING_CONTEXT_DEV)。
# 代码更新走正常渠道:生产用 `/plugin update`;开发设 ROLLING_CONTEXT_DEV=<仓库根> 直接跑仓库。
#
# Usage (inside Claude Code):
#   ! powershell -NoProfile -ExecutionPolicy Bypass -File "<...>\hooks\refresh-proxy.ps1"
$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$ConfigFile = Join-Path $ClaudeDir "rolling-context.json"
$Cfg = $null
if (Test-Path $ConfigFile) { try { $Cfg = Get-Content $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $Cfg = $null } }
$Port = if ($Cfg -and $Cfg.port) { [int]"$($Cfg.port)" } elseif ($env:ROLLING_CONTEXT_PORT) { [int]$env:ROLLING_CONTEXT_PORT } else { 5588 }

function Get-Listeners($p) {
    try {
        return Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop |
               Select-Object -ExpandProperty OwningProcess -Unique
    } catch {
        return netstat -ano | Select-String ":$p\s" | Select-String "LISTENING" |
               ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique
    }
}

# 1) 杀掉占用端口的监听进程(按端口找真正的 listener,绕开 pidfile 可能记错的问题)。
foreach ($pp in Get-Listeners $Port) {
    if ($pp) { Write-Output "[refresh] kill listener PID $pp"; Stop-Process -Id $pp -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Seconds 1

# 2) 重跑标准启动器:它会从 cache(或 ROLLING_CONTEXT_DEV 指定的仓库)起新代理,
#    并由 server.py 自写权威 pidfile/version、自答 /health。
$dev = if ($env:ROLLING_CONTEXT_DEV) { " [DEV]" } else { "" }
Write-Output "[refresh] relaunching gateway via start-proxy.ps1$dev ..."
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir "start-proxy.ps1") | Out-Null

# 3) 报告:问 /health 确认起来了。
Start-Sleep -Seconds 1
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -ErrorAction Stop
    Write-Output "[refresh] OK   gateway ready on port $Port (v$($h.version), PID $($h.pid))"
} catch {
    Write-Output "[refresh] WARN no healthy gateway on port $Port; check $ClaudeDir\rolling-context-proxy.log"
}
