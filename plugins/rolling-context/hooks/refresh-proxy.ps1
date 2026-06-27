# One-shot refresh of the global gateway (Windows).
#
# This is the ONLY action needed to "update the gateway": pull the marketplace clone's
# latest code, then restart the global 5588 gateway.
#   - No /plugin update needed (the gateway runs from the clone, not the cached version dir)
#   - No Claude Code restart needed (the gateway is a standalone process; restarting it applies)
#   - All Claude Code clients share the same 5588 gateway, so one refresh updates everyone
#
# Usage (inside Claude Code):
#   ! powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\.claude\plugins\cache\konijiwa-plugin\rolling-context\<ver>\hooks\refresh-proxy.ps1"
$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$PidFile = Join-Path $ClaudeDir "rolling-context-proxy.pid"
$VerFile = Join-Path $ClaudeDir "rolling-context-proxy.version"
$ConfigFile = Join-Path $ClaudeDir "rolling-context.json"

$Cfg = $null
if (Test-Path $ConfigFile) { try { $Cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json } catch { $Cfg = $null } }
$Port = if ($Cfg -and $Cfg.port) { [int]"$($Cfg.port)" } elseif ($env:ROLLING_CONTEXT_PORT) { [int]$env:ROLLING_CONTEXT_PORT } else { 5588 }

# Resolve the clone repo root (the marketplace git repo). $ScriptDir may be under cache/ or under the clone.
$CloneRoot = ""
if ($ScriptDir -match '\\cache\\') {
    $PluginsRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\..\..\..\.."))
    $MpName = Split-Path ([System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\..\..")))  -Leaf
    $CloneRoot = Join-Path $PluginsRoot "marketplaces\$MpName"
} elseif ($ScriptDir -match '\\marketplaces\\') {
    $CloneRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\..\.."))
}

# 1) Pull latest (best-effort; --ff-only fails on divergence but never damages the clone).
if ($CloneRoot -and (Test-Path (Join-Path $CloneRoot ".git"))) {
    Write-Output "[refresh] git pull --ff-only  ($CloneRoot)"
    git -C "$CloneRoot" pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Output "[refresh] pull skipped/failed, restarting with current clone code" }
} else {
    Write-Output "[refresh] no clone repo found, restarting with current code"
}

# 2) Kill the running gateway BY PORT - OwningProcess is the real listener, sidestepping the
#    git-bash wrapper-PID problem where the pidfile records the wrong process.
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

# 3) Relaunch via the standard launcher (start-proxy resolves the clone source automatically).
Write-Output "[refresh] relaunching gateway..."
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir "start-proxy.ps1") | Out-Null

# 4) Self-heal the pidfile: write the PID that is actually listening on the port.
Start-Sleep -Seconds 2
$real = (Get-Listeners $Port | Select-Object -First 1)
if ($real) {
    Set-Content -Path $PidFile -Value "$real" -NoNewline
    $v = (Get-Content $VerFile -ErrorAction SilentlyContinue)
    Write-Output "[refresh] OK   gateway ready on port $Port, PID $real (version $v)"
} else {
    Write-Output "[refresh] WARN no listener on port $Port; check $ClaudeDir\rolling-context-proxy.log"
}
