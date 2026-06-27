# 一键刷新全局网关(Windows)。
#
# 这是「更新网关」的唯一动作:拉取 marketplace clone 的最新代码,然后重启那个全局 5588 网关。
#   - 不需要 /plugin update(网关从 clone 跑,不从 cache 版本目录跑)
#   - 不需要重启 Claude Code(网关是独立进程,重启它即生效)
#   - 所有 Claude Code 客户端共用同一个 5588 网关,刷新一次,全体立即用上新代码
#
# 用法:在 Claude Code 里输入
#   ! powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\.claude\plugins\cache\konijiwa-plugin\rolling-context\<版本>\hooks\refresh-proxy.ps1"
$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$PidFile = Join-Path $ClaudeDir "rolling-context-proxy.pid"
$VerFile = Join-Path $ClaudeDir "rolling-context-proxy.version"
$ConfigFile = Join-Path $ClaudeDir "rolling-context.json"

$Cfg = $null
if (Test-Path $ConfigFile) { try { $Cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json } catch { $Cfg = $null } }
$Port = if ($Cfg -and $Cfg.port) { [int]"$($Cfg.port)" } elseif ($env:ROLLING_CONTEXT_PORT) { [int]$env:ROLLING_CONTEXT_PORT } else { 5588 }

# clone 仓库根(marketplace git 仓库)。$ScriptDir 可能在 cache 下,也可能就在 clone 下。
$CloneRoot = ""
if ($ScriptDir -match '\\cache\\') {
    $PluginsRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\..\..\..\.."))
    $MpName = Split-Path ([System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\..\..")))  -Leaf
    $CloneRoot = Join-Path $PluginsRoot "marketplaces\$MpName"
} elseif ($ScriptDir -match '\\marketplaces\\') {
    $CloneRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\..\.."))
}

# 1) 拉最新(尽力而为;--ff-only 在分叉时失败但不破坏 clone)。
if ($CloneRoot -and (Test-Path (Join-Path $CloneRoot ".git"))) {
    Write-Output "[refresh] git pull --ff-only  ($CloneRoot)"
    git -C "$CloneRoot" pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Output "[refresh] pull 跳过/失败,用现有 clone 代码重启" }
} else {
    Write-Output "[refresh] 未发现 clone 仓库,直接用现有代码重启"
}

# 2) 按端口杀掉在跑的网关 —— OwningProcess 是真正的监听进程,绕开包装层 PID 问题。
function Get-Listeners($p) {
    $found = @()
    try {
        $found = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop |
                 Select-Object -ExpandProperty OwningProcess -Unique
    } catch {
        $found = netstat -ano | Select-String ":$p\s" | Select-String "LISTENING" |
                 ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique
    }
    return $found
}
foreach ($pp in Get-Listeners $Port) {
    if ($pp) { Write-Output "[refresh] kill listener PID $pp"; Stop-Process -Id $pp -Force -ErrorAction SilentlyContinue }
}
Remove-Item $PidFile, $VerFile -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# 3) 用标准启动器从 clone 重新拉起(start-proxy 会自动解析 clone 源)。
Write-Output "[refresh] 重新启动网关..."
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir "start-proxy.ps1") | Out-Null

# 4) 自愈 pidfile:写入真正监听该端口的 PID。
Start-Sleep -Seconds 2
$real = (Get-Listeners $Port | Select-Object -First 1)
if ($real) {
    Set-Content -Path $PidFile -Value "$real" -NoNewline
    $v = (Get-Content $VerFile -ErrorAction SilentlyContinue)
    Write-Output "[refresh] OK  网关已就绪,监听 $Port,PID $real(版本 $v)"
} else {
    Write-Output "[refresh] WARN 未检测到 $Port 监听,请查看 $ClaudeDir\rolling-context-proxy.log"
}
