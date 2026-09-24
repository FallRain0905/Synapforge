param(
    [switch]$SkipInfra,
    [int]$ApiPort = 8000,
    [int]$WebPort = 3000
)

# 第一版 Demo 一键启动：
#   1) 用户态 PostgreSQL + MinIO（scripts/local-infra.ps1 start）
#   2) FastAPI 控制平面（uvicorn）
#   3) Next.js 工作台（生产构建后 next start）
# 结束后按 Ctrl+C 即可；API/Web 会在退出时被清理。

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot | Split-Path -Parent
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'python 未安装或不在 PATH 中' }

function Wait-Http([string]$url, [int]$seconds = 40) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { return $true }
        } catch { Start-Sleep -Milliseconds 700 }
    }
    return $false
}

Write-Output '=== 1/3 基础设施 ==='
if (-not $SkipInfra) {
    & (Join-Path $PSScriptRoot 'local-infra.ps1') start
} else {
    Write-Output '已跳过基础设施启动'
}

Write-Output '=== 2/3 API 控制平面 ==='
# Windows PowerShell 5.1 的 Start-Process 没有 -Environment，改由父进程设置、子进程继承。
$env:PYTHONPATH = "$root;$root\apps\api"
$apiProcess = Start-Process -FilePath $python -ArgumentList @(
    '-X', 'utf8', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', $ApiPort
) -WorkingDirectory (Join-Path $root 'apps\api') -PassThru -WindowStyle Hidden

if (-not (Wait-Http "http://127.0.0.1:$ApiPort/health")) {
    Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue
    throw "API 未能在 $ApiPort 端口就绪"
}
Write-Output "API 就绪: http://127.0.0.1:$ApiPort/docs"

Write-Output '=== 3/3 Web 工作台 ==='
$webDir = Join-Path $root 'apps\web'
# NEXT_PUBLIC_* 在构建期被内联，因此构建与启动前都要设置。
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:$ApiPort"
if (-not (Test-Path (Join-Path $webDir '.next'))) {
    Write-Output '首次运行，执行生产构建（约 40 秒）...'
    Push-Location $webDir
    try { npm run build | Out-Null } finally { Pop-Location }
}
$webProcess = Start-Process -FilePath 'npm.cmd' -ArgumentList @('run', 'start', '--', '-p', $WebPort) `
    -WorkingDirectory $webDir -PassThru -WindowStyle Hidden

if (-not (Wait-Http "http://127.0.0.1:$WebPort/")) {
    Stop-Process -Id $webProcess.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue
    throw "Web 未能在 $WebPort 端口就绪"
}

Write-Output ''
Write-Output '=== Demo 已就绪 ==='
Write-Output "工作台      : http://127.0.0.1:$WebPort"
Write-Output "API 文档    : http://127.0.0.1:$ApiPort/docs"
Write-Output "竞赛模板包  : http://127.0.0.1:$ApiPort/api/competition-packs"
Write-Output "文档三层    : http://127.0.0.1:$ApiPort/api/documents/layers"
Write-Output "MinIO 控制台: http://127.0.0.1:9101 (platform / platform-dev-only)"
Write-Output ''
Write-Output '按 Ctrl+C 结束 Demo（将清理 API 与 Web 进程）。'

try {
    Wait-Process -Id $apiProcess.Id
} finally {
    foreach ($process in @($apiProcess, $webProcess)) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Output 'Demo 进程已停止（基础设施仍在运行，可用 scripts/local-infra.ps1 stop 关闭）。'
}