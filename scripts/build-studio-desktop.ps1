# 桌面端一键构建（P3）
#
#   .\scripts\build-studio-desktop.ps1 [-SkipCore] [-SkipInstall]
#
# 产物：
#   dist-studio-core\synapforge-core\                     内核（ONEDIR）
#   apps\studio-desktop\dist\synapforge-studio-setup-0.1.0-x64.exe   NSIS 安装包
#   apps\studio-desktop\dist\win-unpacked\                 免安装版（验收用）

param(
    [switch]$SkipCore,
    [switch]$SkipInstall
)

# 与 build-desktop.ps1 一致：PS 5.1 会把原生命令的 stderr 当终止性错误。
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot | Split-Path -Parent
$app = Join-Path $root 'apps\studio-desktop'
$core = Join-Path $root 'dist-studio-core\synapforge-core\synapforge-core.exe'

if (-not $SkipCore) {
    Write-Output '=== 1/3 打包内核 ==='
    & (Join-Path $PSScriptRoot 'build-studio-core.ps1')
    if (-not (Test-Path $core)) { throw '内核打包失败' }
}
if (-not (Test-Path $core)) {
    throw "内核不存在：$core（先跑 scripts\build-studio-core.ps1）"
}

# 国内直连 github.com 会被重置；electron 与 electron-builder 的二进制都走镜像。
$env:ELECTRON_MIRROR = 'https://npmmirror.com/mirrors/electron/'
$env:ELECTRON_BUILDER_BINARIES_MIRROR = 'https://npmmirror.com/mirrors/electron-builder-binaries/'

if (-not $SkipInstall) {
    Write-Output '=== 2/3 安装壳依赖 ==='
    Push-Location $app
    try {
        npm install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm install 失败（exit $LASTEXITCODE）" }
    } finally {
        Pop-Location
    }
}

Write-Output '=== 3/3 electron-builder（NSIS）==='
Push-Location $app
try {
    npm run build:win
    if ($LASTEXITCODE -ne 0) { throw "electron-builder 失败（exit $LASTEXITCODE）" }
} finally {
    Pop-Location
}

$installer = Get-ChildItem (Join-Path $app 'dist\*.exe') -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $installer) { throw '没有找到安装包' }
$size = [math]::Round($installer.Length / 1MB, 1)
Write-Output "安装包：$($installer.FullName)（$size MB）"
$unpacked = Join-Path $app 'dist\win-unpacked'
if (Test-Path $unpacked) { Write-Output "免安装版：$unpacked" }
