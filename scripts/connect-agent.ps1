# 一键接入本地 Agent（UX-4-04）
#
# 设计目标：用户只需要「平台地址 + Agent 显示名 + 向导页给的配对串」即可完成接入，
# 不需要手工编造 session_id / connection_id，也不需要自己生成 Ed25519 密钥。
#
# 用法（配对串从 Web 的「设备与接入」页复制）：
#   .\scripts\connect-agent.ps1 -Url http://127.0.0.1:8000 -AgentName "我的工作站" -Pairing <PAIRING_BLOB>
#
# 可选：
#   -AgentId         Agent 标识（默认由主机名派生：agent-<主机名小写>）
#   -DeviceId        设备标识（默认 device-<主机名小写>）
#   -KeyDirectory    密钥目录（默认 $HOME\.math-agent-platform\keys）
#   -SkipCredential  跳过写入 Windows 凭据管理器（调试用；此时 token 只在内存里）
#   -Force           跳过「设备已存在」预检（仅在确认平台上没有该 device_id 时使用）
#   -Start           注册完成后立刻启动 Gateway 连接
#
# 流程：登记 Agent（幂等）→ keygen（已有则复用）→ device-register（私钥签名）→ 凭据入库 → 打印/启动 Gateway

param(
    [string]$Url = 'http://127.0.0.1:8000',
    [Parameter(Mandatory = $true)][string]$AgentName,
    [Parameter(Mandatory = $true)][string]$Pairing,
    [string]$AgentId,
    [string]$DeviceId,
    [string]$KeyDirectory = (Join-Path $HOME '.math-agent-platform\keys'),
    [string]$KeyName,
    [switch]$SkipCredential,
    [switch]$Force,
    [switch]$Start,
    # 把本机 Codex CLI 作为执行体一起配好（自动探测路径并写入 worker 命令）
    [switch]$Codex
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot | Split-Path -Parent
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'python 未安装或不在 PATH 中' }

$hostSlug = ($env:COMPUTERNAME.ToLower() -replace '[^a-z0-9-]', '-')
if (-not $AgentId) { $AgentId = "agent-$hostSlug" }
if (-not $DeviceId) { $DeviceId = "device-$hostSlug" }
# 密钥名默认跟随设备 id：平台强制"一个公钥只能注册一台设备"
# （拿同一个公钥去注册别的设备会被拒：device_public_key_already_registered）。
if (-not $KeyName) { $KeyName = $DeviceId }

# 配对串是 base64url(JSON)：{pairing_id, pairing_code, challenge, expires_at}
function ConvertFrom-Base64Url([string]$value) {
    $padded = $value.Replace('-', '+').Replace('_', '/')
    switch ($padded.Length % 4) {
        2 { $padded += '==' }
        3 { $padded += '=' }
        1 { throw '配对串无效（长度不合法）' }
    }
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($padded))
}

$pairingJson = $null
try {
    $pairingJson = ConvertFrom-Base64Url $Pairing | ConvertFrom-Json
} catch {
    throw "配对串无法解析：$($_.Exception.Message)"
}
foreach ($field in @('pairing_id', 'pairing_code', 'challenge')) {
    if (-not $pairingJson.$field) { throw "配对串缺少字段 $field，请在向导页重新生成" }
}
if ($pairingJson.expires_at) {
    $expires = [datetime]::Parse($pairingJson.expires_at).ToUniversalTime()
    if ($expires -lt [datetime]::UtcNow) { throw "配对已过期（$($pairingJson.expires_at)），请在向导页重新生成" }
}

Write-Output '=== 1/5 登记 Agent（幂等）==='
# 设备注册要求 Agent 已存在且归属与配对创建者一致；register 是 upsert，可重复执行。
$registerAgentArgs = @(
    '-X', 'utf8', (Join-Path $root 'apps\agent\agentd.py'), '--url', $Url, 'register',
    '--agent-id', $AgentId, '--display-name', $AgentName, '--workspace', $root
)
& $python @registerAgentArgs | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Agent 登记失败：请检查平台地址 $Url 是否可达" }
Write-Output "Agent 已登记：$AgentId"

Write-Output '=== 2/5 设备身份密钥 ==='
New-Item -ItemType Directory -Force -Path $KeyDirectory | Out-Null
$keyPath = Join-Path $KeyDirectory "$KeyName.key"
if (Test-Path $keyPath) {
    Write-Output "已存在密钥，复用：$keyPath"
} else {
    $keygen = & $python -X utf8 (Join-Path $root 'apps\agent\agentd.py') keygen --directory $KeyDirectory --name $KeyName | ConvertFrom-Json
    Write-Output "已生成密钥：$($keygen.private_key_path)"
    Write-Output "公钥指纹：$($keygen.public_key_fingerprint)"
}

Write-Output '=== 3/5 设备注册（私钥签名）==='
# 一个 device_id 只能注册一次（服务端另外校验公钥指纹唯一）。
# 先查一次，把「已接入过」变成可执行的提示，而不是让用户撞一个 409。
if (-not $Force) {
    $existing = $null
    try {
        $existingDevices = Invoke-RestMethod -Uri "$($Url.TrimEnd('/'))/api/devices" -TimeoutSec 10
        $existing = $existingDevices | Where-Object { $_.device_id -eq $DeviceId } | Select-Object -First 1
    } catch {
        Write-Output "提示：读不到设备列表（$($_.Exception.Message)），跳过重复接入检查。"
    }
    if ($existing) {
        $state = if ($existing.status -eq 'active') { '有效' } else { '已撤销' }
        throw @"
设备 $DeviceId 在平台上已存在（状态 $state），不能重复注册。
  只想重启连接：直接执行第 5 步打印的 gateway-run 命令，不需要重新配对（Token 仍在凭据管理器里）；
  密钥丢了或要换机器：先在 Web 的「设备与接入」页撤销这台设备，再用新配对重新执行本脚本；
  或者给本次接入换一个设备标识：-DeviceId <新的设备标识>（密钥名会自动跟随设备标识，不会复用旧公钥）。
"@
    }
}
$registerArgs = @(
    '-X', 'utf8', (Join-Path $root 'apps\agent\agentd.py'), '--url', $Url, 'device-register',
    # 用 --opt=value 形式：challenge 是 base64url 令牌，可能以 '-' 或 '_' 开头，
    # 空格分隔传给 argparse 时会被当成选项（实测约 3% 的配对会随机踩到）。
    "--pairing-code=$($pairingJson.pairing_code)",
    "--pairing-id=$($pairingJson.pairing_id)",
    "--challenge=$($pairingJson.challenge)",
    '--agent-id', $AgentId,
    '--device-id', $DeviceId,
    '--device-name', $AgentName,
    '--private-key', $keyPath,
    '--platform', 'windows',
    '--agent-version', '0.1.0',
    '--capabilities', 'task.claim'
)
$env:PYTHONPATH = "$root;$root\apps\agent;$root\apps\api"
$registerRaw = $null
try {
    $registerRaw = & $python @registerArgs
} catch {
    throw "设备注册失败：$($_.Exception.Message)"
}
if ($LASTEXITCODE -ne 0) {
    throw @'
设备注册失败（原因见上一行 http_<状态码>:<服务端 detail>）。常见原因与处理：
  device_public_key_already_registered —— 该公钥已注册过别的设备：
                                          换 -DeviceId（密钥名会跟着变），或先在平台撤销旧设备；
  device_pairing_not_pending           —— 配对码已被用过：在向导页重新生成一个；
  device_pairing_expired               —— 配对码超过 15 分钟有效期：重新生成；
  device_pairing_challenge_invalid     —— 配对串内容与服务端记录不一致：重新复制配对串；
  device_agent_owner_mismatch          —— Agent 归属与配对创建者不是同一个成员：
                                          用同一成员账号创建 Agent 与配对。
'@
}
$register = $registerRaw | ConvertFrom-Json
Write-Output "设备已登记：$($register.device.device_id) · 指纹 $($register.device.public_key_fingerprint)"

Write-Output '=== 4/5 保存设备 Token ==='
# 设备 Token 只在注册响应里出现一次，平台只存哈希，无法回显。
if ($SkipCredential) {
    Write-Output '已跳过凭据入库（-SkipCredential）：Token 不会持久化，Gateway 需要 --device-token 才能连接。'
} else {
    $register.device_token | & $python -X utf8 (Join-Path $root 'apps\agent\agentd.py') credential-save --device-id $DeviceId --token-stdin
    if ($LASTEXITCODE -ne 0) { throw '凭据写入失败（见上面的错误）' }
    Write-Output "Token 已写入 Windows 凭据管理器：MathAgentPlatform/device-token/$DeviceId"
}

$codexPath = $null
if ($Codex) {
    Write-Output '=== 5/5 Codex 执行体 ==='
    # 顺序：CODEX_CLI_PATH → ChatGPT 桌面端解包目录（取最新）→ PATH
    if ($env:CODEX_CLI_PATH -and (Test-Path $env:CODEX_CLI_PATH)) {
        $codexPath = $env:CODEX_CLI_PATH
    }
    if (-not $codexPath -and $env:LOCALAPPDATA) {
        $binRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
        if (Test-Path $binRoot) {
            $candidate = Get-ChildItem -Path $binRoot -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName 'codex.exe' } |
                Where-Object { Test-Path $_ } |
                Sort-Object { (Get-Item $_).LastWriteTime } -Descending |
                Select-Object -First 1
            if ($candidate) { $codexPath = $candidate }
        }
    }
    if (-not $codexPath) {
        $onPath = Get-Command codex.exe -ErrorAction SilentlyContinue
        if ($onPath) { $codexPath = $onPath.Source }
    }
    if ($codexPath) {
        $version = (& $codexPath --version 2>&1 | Select-Object -First 1)
        Write-Output "已找到 Codex CLI：$codexPath"
        Write-Output "版本：$version"
        Write-Output '任务侧用法：resource_policy 里写 worker_executor="codex" 与 worker_prompt="<提示词>"，'
        Write-Output '  或直接把 worker_command 指向该可执行文件；sandbox 默认 read-only（danger-full-access 会被平台拒绝）。'
        Write-Output '首次跑 worker（带 --grant）时会把该路径记进 worker.json，之后免填；未显式配置时 worker 也会按同样顺序自动探测。'
    } else {
        Write-Output '未找到 Codex CLI：请安装 ChatGPT 桌面端或 Codex CLI，或用 -CodexPath 指定（本脚本暂不校验）。'
    }
} else {
    Write-Output '=== 5/5 Gateway 连接 ==='
}

$(if ($Codex) { Write-Output '=== 6/6 Gateway 连接 ===' })
$sessionId = "session-$DeviceId"
$connectionId = "conn-$([guid]::NewGuid().ToString('N').Substring(0, 12))"
$wsUrl = ($Url -replace '^http', 'ws') + "/ws/agents/$DeviceId`?session_id=$sessionId&connection_id=$connectionId"
$gatewayArgs = @(
    '-X', 'utf8', (Join-Path $root 'apps\agent\agentd.py'), 'gateway-run',
    '--uri', $wsUrl, '--device-id', $DeviceId, '--agent-id', $AgentId,
    '--session-id', $sessionId, '--connection-id', $connectionId
)
if ($Start) {
    Write-Output "启动 Gateway：$connectionId"
    & $python @gatewayArgs
} else {
    Write-Output '接入完成。启动 Gateway 连接（保持前台运行）：'
    Write-Output ''
    # 打印可直接粘贴的整行命令（含解释器），而不是参数数组。
    $quoted = $gatewayArgs | ForEach-Object { if ($_ -match '[\s]') { "`"$_`"" } else { $_ } }
    Write-Output ("python " + ($quoted -join ' '))
    Write-Output ''
    Write-Output '提示：连接断开后平台会在 90 秒内把该 Agent 标记为离线。'
}