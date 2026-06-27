# Ensure rolling context proxy is running (Windows)
# Pure stdlib — no venv needed, just python

$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# 网关代码源:优先用 marketplace clone(单一最新源——`/plugin marketplace update` 或 `git pull`
# 刷新它即可,更新网关无需 /plugin update 重新 cache、无需重启 CC)。找不到 clone 时回退到本地
# cache 副本(可移植)。$ScriptDir 形如 <plugins>\cache\<MP>\rolling-context\<VER>\hooks。
$ProxyDir = Join-Path $ScriptDir "..\proxy"
$SrcPluginJson = Join-Path $ScriptDir "..\.claude-plugin\plugin.json"
if ($ScriptDir -match '\\cache\\') {
    $PluginsRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\..\..\..\.."))
    $MpName = Split-Path ([System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\..\..")))  -Leaf
    $Cand = Join-Path $PluginsRoot "marketplaces\$MpName\plugins\rolling-context"
    if (Test-Path (Join-Path $Cand "proxy\server.py")) {
        $ProxyDir = Join-Path $Cand "proxy"
        $SrcPluginJson = Join-Path $Cand ".claude-plugin\plugin.json"
    }
}
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$PidFile = Join-Path $ClaudeDir "rolling-context-proxy.pid"
$VerFile = Join-Path $ClaudeDir "rolling-context-proxy.version"
$HookLog = Join-Path $ClaudeDir "rolling-context-hook.log"
$ProxyLog = Join-Path $ClaudeDir "rolling-context-proxy.log"
# 第三方 baseURL 配置（显式、稳定；存在时为权威，不再从 ANTHROPIC_BASE_URL 推导，杜绝来回横跳）。
$ConfigFile = Join-Path $ClaudeDir "rolling-context.json"
$Cfg = $null
if (Test-Path $ConfigFile) { try { $Cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json } catch { $Cfg = $null } }
$Port = if ($Cfg -and $Cfg.port) { "$($Cfg.port)" } elseif ($env:ROLLING_CONTEXT_PORT) { $env:ROLLING_CONTEXT_PORT } else { "5588" }
$ProxyUrl = "http://127.0.0.1:$Port"
$HasConfigUpstream = ($null -ne $Cfg) -and ($null -ne $Cfg.upstream) -and ($Cfg.upstream -ne "")
$CurrentVersion = if (Test-Path $SrcPluginJson) { (Get-Content $SrcPluginJson -Raw | ConvertFrom-Json).version } else { "unknown" }

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $HookLog -Value "[$ts] $msg"
}

Log "Hook started. ProxyDir=$ProxyDir"

# Always update settings.json first (even if proxy is already running)
$SettingsFile = Join-Path $ClaudeDir "settings.json"
try {
    if (Test-Path $SettingsFile) {
        $settings = Get-Content $SettingsFile -Raw | ConvertFrom-Json
    } else {
        $settings = [PSCustomObject]@{}
    }

    # Ensure env object exists
    if (-not ($settings | Get-Member -Name "env" -MemberType NoteProperty)) {
        $settings | Add-Member -NotePropertyName "env" -NotePropertyValue ([PSCustomObject]@{})
    }

    if ($HasConfigUpstream) {
        # 权威模式：上游来自 rolling-context.json（server.py 直接读），这里只强制把 claude 指向本地代理。
        $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue $ProxyUrl -Force
        Log "Authoritative: ANTHROPIC_BASE_URL=$ProxyUrl upstream=$($Cfg.upstream) (config)"
    } else {
        $existingUrl = $null
        if ($settings.env | Get-Member -Name "ANTHROPIC_BASE_URL" -MemberType NoteProperty) {
            $existingUrl = $settings.env.ANTHROPIC_BASE_URL
        }
        if (-not $existingUrl) {
            $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue $ProxyUrl -Force
            Log "Set ANTHROPIC_BASE_URL=$ProxyUrl (settings.json)"
        } elseif ($existingUrl -notmatch "127\.0\.0\.1.*$Port") {
            $settings.env | Add-Member -NotePropertyName "ROLLING_CONTEXT_UPSTREAM" -NotePropertyValue $existingUrl -Force
            $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue $ProxyUrl -Force
            Log "Chaining: upstream=$existingUrl (settings.json)"
        } else {
            Log "ANTHROPIC_BASE_URL already set (settings.json)"
        }

        # 仅无 config 时写 env 默认（有 config 时由 server.py 从 config 读，不污染 settings.json）。
        $defaults = @{
            "ROLLING_CONTEXT_PORT"    = "5588"
            "ROLLING_CONTEXT_TRIGGER" = "160000"
            "ROLLING_CONTEXT_TARGET"  = "40000"
            "ROLLING_CONTEXT_MODEL"   = "claude-haiku-4-5-20251001"
        }
        foreach ($key in $defaults.Keys) {
            if (-not ($settings.env | Get-Member -Name $key -MemberType NoteProperty)) {
                $settings.env | Add-Member -NotePropertyName $key -NotePropertyValue $defaults[$key]
            }
        }
    }

    # 写 UTF8 无 BOM：PS5.1 的 Set-Content -Encoding UTF8 会带 BOM，
    # 导致代理(server.py)用 utf-8 读 settings.json 解析失败、退回默认上游。
    $json = $settings | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($SettingsFile, $json, (New-Object System.Text.UTF8Encoding($false)))
} catch {
    Log "WARNING: Could not update settings.json: $_"
}

# Check if proxy is already running
if (Test-Path $PidFile) {
    $savedPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($savedPid) {
        $proc = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if ($proc) {
            # 版本闸门:同版本复用;在跑的版本 >= 本会话版本则复用(绝不降级);
            # 仅当本会话版本严格更高才重启升级。根治多版本会话把 5588 共享代理来回拽、
            # 每次重启掐断在传请求 + 清空压缩状态的「版本互踢」。
            $runningVersion = "$(if (Test-Path $VerFile) { Get-Content $VerFile -ErrorAction SilentlyContinue } else { '' })".Trim()
            if ($runningVersion -eq $CurrentVersion) {
                Log "Proxy already running (PID $savedPid, v$runningVersion)"
                exit 0
            }
            $isUpgrade = $true   # 版本号无法比较时(缺失/unknown)按旧行为重启升级
            try {
                if ($runningVersion -and $CurrentVersion -and $CurrentVersion -ne 'unknown') {
                    $isUpgrade = ([version]$CurrentVersion).CompareTo([version]$runningVersion) -gt 0
                }
            } catch { $isUpgrade = $true }
            if (-not $isUpgrade) {
                Log "Proxy running newer/equal (PID $savedPid, v$runningVersion >= v$CurrentVersion) - reusing, no downgrade"
                exit 0
            }
            Log "Upgrading proxy ($runningVersion -> $CurrentVersion), restarting proxy (PID $savedPid)"
            Stop-Process -Id $savedPid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item $VerFile -Force -ErrorAction SilentlyContinue
}

# 兜底去重:无论 PidFile 是否准确,起新代理前先清掉任何仍在监听本端口的残留进程,
# 根治「PidFile 被污染(如手动跑过 .sh 记成包装层 PID)→ 旧代理没杀掉 + 起新的 → 双实例抢端口」。
# 注:同版本健康代理已在上面的 PidFile 检查里 exit 0,走不到这里,不会误杀健康实例。
try {
    $listeners = Get-NetTCPConnection -State Listen -LocalPort ([int]$Port) -ErrorAction SilentlyContinue
    $killed = $false
    foreach ($conn in $listeners) {
        $opid = $conn.OwningProcess
        if ($opid -and $opid -ne 0) {
            Log "Killing stale listener on port $Port (PID $opid)"
            Stop-Process -Id $opid -Force -ErrorAction SilentlyContinue
            $killed = $true
        }
    }
    if ($killed) { Start-Sleep -Milliseconds 500 }
} catch {
    Log "Port cleanup skipped: $_"
}

# Start proxy directly with system python — no venv needed
Log "Starting proxy..."
$proc = Start-Process -FilePath "python" -ArgumentList "server.py" `
    -WorkingDirectory $ProxyDir `
    -RedirectStandardOutput $ProxyLog -RedirectStandardError "$ProxyLog.err" `
    -WindowStyle Hidden -PassThru
$proc.Id | Out-File -FilePath $PidFile -NoNewline
$CurrentVersion | Out-File -FilePath $VerFile -NoNewline
Log "Proxy started with PID $($proc.Id) (v$CurrentVersion)"

exit 0
