# 桌面端工具链自检（DP-0-04）
#
#   .\scripts\desktop-toolchain-check.ps1
#
# 逐项报告构建桌面端所需的工具是否就绪，并给出缺失项的安装指引。
# 只读检查，不安装任何东西。

$ErrorActionPreference = 'Continue'

$script:checks = @()
function Report([string]$name, [bool]$ok, [string]$detail, [string]$hint = '') {
    $script:checks += [pscustomobject]@{ name = $name; ok = $ok; detail = $detail; hint = $hint }
    $mark = if ($ok) { 'OK  ' } else { 'MISS' }
    Write-Output ("  [{0}] {1} — {2}" -f $mark, $name, $detail)
    if (-not $ok -and $hint) { Write-Output ("          提示：{0}" -f $hint) }
}

function Test-Command([string]$exe) {
    $found = Get-Command $exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return $null
}

Write-Output '=== 桌面端工具链自检 ==='
Write-Output '（只读检查；不安装任何东西）'
Write-Output ''

# ---- 必需：Node / npm（Electron 壳）----
Write-Output '--- 壳层（Electron）---'
$node = Test-Command 'node'
if ($node) {
    $nodeVersion = (& node --version) 2>$null
    Report 'node' $true "$nodeVersion ($node)"
} else {
    Report 'node' $false '未找到' '安装 Node.js LTS：https://nodejs.org/'
}
$npm = Test-Command 'npm'
if ($npm) {
    $npmVersion = (& npm --version) 2>$null
    Report 'npm' $true "v$npmVersion"
} else {
    Report 'npm' $false '未找到' '随 Node.js 一起安装'
}

# ---- 必需：Python（内核）----
Write-Output ''
Write-Output '--- 内核（Python）---'
$python = Test-Command 'python'
if ($python) {
    $pyVersion = (& python --version) 2>$null
    Report 'python' $true "$pyVersion ($python)"
    # 内核依赖自检：cryptography（设备身份签名）与 websockets（Gateway）
    $deps = & python -c "import importlib.util as u; print(','.join(m for m in ['cryptography','websockets','fastapi'] if u.find_spec(m) is None))" 2>$null
    if ([string]::IsNullOrWhiteSpace($deps)) {
        Report 'python 依赖' $true 'cryptography / websockets / fastapi 均可用'
    } else {
        Report 'python 依赖' $false "缺少：$deps" 'pip install -r apps/agent/requirements.txt'
    }
} else {
    Report 'python' $false '未找到' '安装 Python 3.11+'
}

# ---- 打包：PyInstaller ----
Write-Output ''
Write-Output '--- 打包与分发 ---'
$pyinstaller = & python -c "import importlib.util as u; print('yes' if u.find_spec('PyInstaller') else 'no')" 2>$null
if ($pyinstaller -eq 'yes') {
    $piVersion = & python -m PyInstaller --version 2>$null
    Report 'PyInstaller' $true "v$piVersion"
} else {
    Report 'PyInstaller' $false '未安装' 'pip install pyinstaller（用于把内核打包成 sidecar）'
}

# ---- 代码签名（DP-4 才需要，缺失不阻塞 DP-1）----
$signtool = Test-Command 'signtool'
if ($signtool) {
    Report 'signtool' $true $signtool
} else {
    Report 'signtool' $false '未找到（DP-4 才需要）' '随 Windows SDK 安装；DP-1 内测版可不签名'
}

# ---- 可选：本机 Agent CLI（执行体）----
Write-Output ''
Write-Output '--- 本机执行体 CLI（可选）---'
$codexCandidates = @()
if ($env:CODEX_CLI_PATH) { $codexCandidates += $env:CODEX_CLI_PATH }
if ($env:LOCALAPPDATA) {
    $binRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    if (Test-Path $binRoot) {
        $codexCandidates += Get-ChildItem -Path $binRoot -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'codex.exe' } |
            Where-Object { Test-Path $_ } |
            Sort-Object { (Get-Item $_).LastWriteTime } -Descending
    }
}
$codexOnPath = Test-Command 'codex'
if ($codexOnPath) { $codexCandidates += $codexOnPath }
$codex = $codexCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($codex) {
    $codexVersion = (& $codex --version 2>&1 | Select-Object -First 1)
    Report 'codex CLI' $true "$codexVersion" 
} else {
    Report 'codex CLI' $false '未找到' '安装 ChatGPT 桌面端（自带 Codex CLI）或独立 Codex CLI'
}
$claude = Test-Command 'claude'
if ($claude) {
    Report 'claude CLI' $true $claude
} else {
    Report 'claude CLI' $false '未找到（可选，Claude 适配器尚未打通）' ''
}

# ---- 汇总 ----
Write-Output ''
$blocking = @($script:checks | Where-Object { -not $_.ok -and $_.name -notin @('signtool', 'codex CLI', 'claude CLI') })
$okCount = @($script:checks | Where-Object { $_.ok }).Count
Write-Output ("通过 {0}/{1} 项；阻塞项 {2} 个" -f $okCount, $script:checks.Count, $blocking.Count)
if ($blocking.Count -eq 0) {
    Write-Output '工具链就绪：可以开始 DP-1（壳与内核骨架）。'
    exit 0
}
Write-Output '存在阻塞项，先补齐再开工：'
$blocking | ForEach-Object { Write-Output ("  - {0}：{1}" -f $_.name, $_.hint) }
exit 1