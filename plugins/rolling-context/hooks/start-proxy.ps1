# Ensure rolling context proxy is running (Windows)
# Pure stdlib — no venv needed, just python.
#
# 生命周期模型(对齐上游原版,并修掉 fail-closed):
#   • 代码默认从本地 cache 副本跑:<...>\rolling-context\<VER>\proxy。CC 通过 `/plugin update`
#     拉新 cache、起新会话即生效 —— CC 掌舵生命周期,hook 只负责「该起就起」。
#   • 作者本地提速:设 ROLLING_CONTEXT_DEV=<仓库根> 则改从仓库跑(免 /plugin update),
#     仅本机本人有用,绝不写进任何全局清单、不污染生产生命周期。
#   • fail-open:仅当代理 /health 真活着才把 ANTHROPIC_BASE_URL 指向它;它起不来就放行到真上游,
#     绝不因代理挂掉而连累整个 Claude Code。

$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1) 选代码源:默认 cache 副本;ROLLING_CONTEXT_DEV 指向仓库时改用仓库(作者本地提速)。
$ProxyDir = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\proxy"))
$DevRoot = $env:ROLLING_CONTEXT_DEV
if ($DevRoot -and (Test-Path (Join-Path $DevRoot "proxy\server.py"))) {
    $ProxyDir = [System.IO.Path]::GetFullPath((Join-Path $DevRoot "proxy"))
}
$SrcPluginJson = Join-Path $ProxyDir "..\.claude-plugin\plugin.json"

$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$PidFile = Join-Path $ClaudeDir "rolling-context-proxy.pid"
$HookLog = Join-Path $ClaudeDir "rolling-context-hook.log"
$ProxyLog = Join-Path $ClaudeDir "rolling-context-proxy.log"
$SettingsFile = Join-Path $ClaudeDir "settings.json"
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

# 判活以「活着的 /health」为唯一权威(自报 version + pid),pidfile 仅兜底。
function Get-Health {
    try {
        return Invoke-RestMethod -Uri "$ProxyUrl/health" -TimeoutSec 2 -ErrorAction Stop
    } catch {
        return $null
    }
}

# 腾位:杀掉占着本端口的代理(先按 /health 自报的 pid,再兜底杀端口上任何残留监听者)。
# 仅在「确证有错版本代理占着端口」时作最后手段调用——稳态复用根本走不到这一步,不会误杀健康实例。
function Stop-PortHolder($holderPid) {
    if ($holderPid) { Stop-Process -Id ([int]$holderPid) -Force -ErrorAction SilentlyContinue }
    try {
        Get-NetTCPConnection -State Listen -LocalPort ([int]$Port) -ErrorAction SilentlyContinue |
            ForEach-Object {
                if ($_.OwningProcess -and $_.OwningProcess -ne 0) {
                    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
                }
            }
    } catch {}
    Start-Sleep -Milliseconds 500
}

Log "Hook started. ProxyDir=$ProxyDir v$CurrentVersion$(if ($DevRoot) { ' [DEV]' })"

# 2) 判活 + 升级闸门:
#    • 在跑版本「可识别且 >= 本会话版本」→ 复用(绝不降级互踢)
#    • 在跑版本更低,或无法识别(如旧代理 /health 不报 version)→ 重启,让新版/新代码顶上
$proxyHealthy = $false
$health = Get-Health
if ($health) {
    $runningVersion = "$($health.version)".Trim()
    $reuse = $false
    if ($runningVersion -and $runningVersion -ne 'unknown' -and $CurrentVersion -and $CurrentVersion -ne 'unknown') {
        try { $reuse = ([version]$runningVersion).CompareTo([version]$CurrentVersion) -ge 0 } catch { $reuse = $false }
    }
    if ($reuse) {
        Log "Proxy healthy (PID $($health.pid), v$runningVersion >= v$CurrentVersion) - reusing"
        $proxyHealthy = $true
    } else {
        $rv = if ($runningVersion) { $runningVersion } else { '?' }
        Log "Restarting proxy (running v$rv -> v$CurrentVersion; stopping PID $($health.pid))"
        if ($health.pid) { Stop-Process -Id ([int]$health.pid) -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
    }
}

# 3) 不健康(或刚为升级停掉)→ 启动一个新代理,轮询等它就绪。
if (-not $proxyHealthy) {
    # 兜底:pidfile 记的进程还活着但 /health 不通(卡死)→ 杀掉再起。
    if (Test-Path $PidFile) {
        $savedPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($savedPid -and (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)) {
            Log "Stopping leftover proxy (PID $savedPid) before relaunch"
            Stop-Process -Id $savedPid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 500
        }
    }
    # server.py 绑定成功后自写 pidfile/version 并答 /health。绑定即锁:若并发起了第二个,
    # 它会 EADDRINUSE 后干净 exit 0,不会双实例。
    # 关键:轮询要确认「起来的确实是本版本」——若旧/异版本代理还占着端口(绑定即锁让我们这发干净退了),
    # /health 会答出别的 version。仅凭这一「确证版本不符」的证据才作最后手段杀端口腾位、再起一发;
    # 最多两发,腾不动就 fail-open。根治原先「轮询见任一健康代理就当成功」→ 老代理赖着端口、却谎报已升级。
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        Log "Starting proxy from $ProxyDir ... (attempt $attempt)"
        Start-Process -FilePath "python" -ArgumentList "server.py" `
            -WorkingDirectory $ProxyDir `
            -RedirectStandardOutput $ProxyLog -RedirectStandardError "$ProxyLog.err" `
            -WindowStyle Hidden | Out-Null
        $squatter = $null
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Milliseconds 500
            $h = Get-Health
            if (-not $h) { continue }
            if ("$($h.version)".Trim() -eq $CurrentVersion) { $proxyHealthy = $true; break }
            $squatter = $h; break   # 有健康代理在答,但版本不是我们 → 占位者
        }
        if ($proxyHealthy) { break }
        if ($squatter) {
            Log "Port $Port held by v$("$($squatter.version)".Trim()) (PID $($squatter.pid)) != v$CurrentVersion - freeing, retrying"
            Stop-PortHolder $squatter.pid
        } else {
            break   # 没人应答 = 真没起来(非占位),别再空转杀端口
        }
    }
    if ($proxyHealthy) {
        Log "Proxy is up (v$CurrentVersion)"
    } else {
        Log "WARNING: proxy did not become healthy in time - failing open to upstream"
    }
}

# 4) 写 settings.json:健康才指向代理;否则 fail-open 放行到真上游(绝不连累 CC)。
try {
    if (Test-Path $SettingsFile) {
        $settings = Get-Content $SettingsFile -Raw | ConvertFrom-Json
    } else {
        $settings = [PSCustomObject]@{}
    }
    if (-not ($settings | Get-Member -Name "env" -MemberType NoteProperty)) {
        $settings | Add-Member -NotePropertyName "env" -NotePropertyValue ([PSCustomObject]@{})
    }

    if ($proxyHealthy) {
        if ($HasConfigUpstream) {
            # 权威模式:上游来自 config（server.py 直接读），这里只把 claude 指向本地代理。
            $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue $ProxyUrl -Force
            Log "BASE_URL=$ProxyUrl upstream=$($Cfg.upstream) (config)"
        } else {
            $existingUrl = $null
            if ($settings.env | Get-Member -Name "ANTHROPIC_BASE_URL" -MemberType NoteProperty) {
                $existingUrl = $settings.env.ANTHROPIC_BASE_URL
            }
            if ($existingUrl -and ($existingUrl -notmatch "127\.0\.0\.1.*$Port")) {
                # 链上已有真上游 → 存起来给 server.py 用,再把 claude 指向代理。
                $settings.env | Add-Member -NotePropertyName "ROLLING_CONTEXT_UPSTREAM" -NotePropertyValue $existingUrl -Force
                Log "Chaining upstream=$existingUrl"
            }
            $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue $ProxyUrl -Force
            # 仅无 config 时写 env 默认(有 config 时由 server.py 从 config 读,不污染 settings.json)。
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
    } else {
        # FAIL-OPEN:代理没起来 → 把 BASE_URL 指回真上游(或移除回落官方 API),让 CC 照常工作。
        $failTarget = $null
        if ($HasConfigUpstream) {
            $failTarget = $Cfg.upstream
        } elseif ($settings.env | Get-Member -Name "ROLLING_CONTEXT_UPSTREAM" -MemberType NoteProperty) {
            $failTarget = $settings.env.ROLLING_CONTEXT_UPSTREAM
        }
        $existingUrl = $null
        if ($settings.env | Get-Member -Name "ANTHROPIC_BASE_URL" -MemberType NoteProperty) {
            $existingUrl = $settings.env.ANTHROPIC_BASE_URL
        }
        if ($failTarget) {
            $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue $failTarget -Force
            Log "FAIL-OPEN: ANTHROPIC_BASE_URL=$failTarget (proxy down)"
        } elseif ($existingUrl -and ($existingUrl -match "127\.0\.0\.1.*$Port")) {
            # 之前指着代理、又没有已知真上游 → 移除,回落官方 API,绝不把 CC 卡死在死代理上。
            $settings.env.PSObject.Properties.Remove("ANTHROPIC_BASE_URL")
            Log "FAIL-OPEN: removed ANTHROPIC_BASE_URL -> default API (proxy down, no known upstream)"
        } else {
            Log "FAIL-OPEN: leaving ANTHROPIC_BASE_URL as-is (proxy down)"
        }
    }

    # 写 UTF8 无 BOM：PS5.1 的 Set-Content -Encoding UTF8 会带 BOM,
    # 导致代理(server.py)用 utf-8 读 settings.json 解析失败、退回默认上游。
    $json = $settings | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($SettingsFile, $json, (New-Object System.Text.UTF8Encoding($false)))
} catch {
    Log "WARNING: Could not update settings.json: $_"
}

exit 0
