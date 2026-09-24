# 桌面端一键构建（DP-1-02 收尾 + DP-1-06）
#
#   .\scripts\build-desktop.ps1 [-SkipSidecar] [-SkipInstall]
#
# 步骤：
#   1) 工具链自检（缺项直接失败，避免构建到一半才报错）
#   2) PyInstaller 打包 Python 内核 → dist-sidecar\math-agent-sidecar\（ONEDIR）
#   3) 安装前端依赖（npm 11 会拦 postinstall，这里显式补跑 electron 的安装脚本）
#   4) electron-builder 产出 NSIS 安装包（未签名，内测用）
#
# 产物：
#   dist-sidecar\math-agent-sidecar\math-agent-sidecar.exe   内核（随安装包分发）
#   apps\desktop\dist\Math Agent Platform Setup 0.1.0.exe    安装包

param(
    [switch]$SkipSidecar,
    [switch]$SkipInstall
)

# 为什么不是 'Stop'：PS 5.1 在 Stop 下会把原生程序写到 stderr 的**任何**输出当成终止性错误
# （PyInstaller/electron-builder 的进度日志都走 stderr），即使重定向也拦不住。
# 因此这里用 'Continue' + 对每个原生调用显式检查 $LASTEXITCODE，失败一样会立刻中止。
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot | Split-Path -Parent
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'python 未安装或不在 PATH 中' }

Write-Output '=== 1/4 工具链自检 ==='
& (Join-Path $PSScriptRoot 'desktop-toolchain-check.ps1')
if ($LASTEXITCODE -ne 0) {
    throw '工具链自检未通过：先补齐上面列出的阻塞项'
}

if (-not $SkipSidecar) {
    Write-Output '=== 2/4 打包 Python 内核（PyInstaller ONEDIR）==='
    $distRoot = Join-Path $root 'dist-sidecar'
    $workRoot = Join-Path $root 'dist-sidecar-build'
    if (Test-Path $distRoot) { Remove-Item -Recurse -Force $distRoot -ErrorAction Stop }
    Push-Location (Join-Path $root 'apps\agent')
    try {
        $env:PYTHONPATH = "$root;$root\apps\agent"
        # 注意：PyInstaller 把进度写 stderr，必须合并流后再判断退出码，
        # 否则 PowerShell 会把 stderr 当错误并直接中断（NativeCommandError）
        $pyLog = & $python -X utf8 -m PyInstaller --noconfirm --clean --onedir `
            --name math-agent-sidecar `
            --distpath $distRoot --workpath $workRoot --specpath $workRoot `
            --hidden-import cryptography --hidden-import websockets `
            sidecar_entry.py *>&1
        if ($LASTEXITCODE -ne 0) {
            $pyLog | Select-Object -Last 15 | ForEach-Object { Write-Output $_ }
            throw "PyInstaller 打包失败（exit $LASTEXITCODE）"
        }
    } finally {
        Pop-Location
    }
    $sidecarExe = Join-Path $distRoot 'math-agent-sidecar\math-agent-sidecar.exe'
    if (-not (Test-Path $sidecarExe)) { throw "内核打包失败：$sidecarExe 不存在" }
    $size = [math]::Round((Get-ChildItem -Recurse $distRoot | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
    Write-Output "内核已打包：$sidecarExe（ONEDIR 合计 $size MB）"
} else {
    Write-Output '=== 2/4 已跳过内核打包 ==='
}

$desktop = Join-Path $root 'apps\desktop'
Push-Location $desktop
try {
    if (-not $SkipInstall) {
        Write-Output '=== 3/4 安装/校验依赖 ==='
        if (-not (Test-Path (Join-Path $desktop 'node_modules'))) {
            $npmLog = npm install --no-audit --no-fund *>&1
            if ($LASTEXITCODE -ne 0) {
                $npmLog | Select-Object -Last 15 | ForEach-Object { Write-Output $_ }
                throw "npm install 失败（exit $LASTEXITCODE）"
            }
        }
        # npm 11 默认不跑安装脚本，Electron 的二进制要显式补跑
        $electronExe = Join-Path $desktop 'node_modules\electron\dist\electron.exe'
        if (-not (Test-Path $electronExe)) {
            Write-Output '  补跑 Electron 二进制安装（npm 11 拦截 postinstall 的绕过）'
            & node (Join-Path $desktop 'node_modules\electron\install.js')
        }
        if (-not (Test-Path $electronExe)) { throw 'Electron 二进制仍缺失' }
        Write-Output '  依赖就绪'
    } else {
        Write-Output '=== 3/4 已跳过依赖安装 ==='
    }

    Write-Output '=== 4/4 生成 NSIS 安装包 ==='
    $buildLog = npx electron-builder --win nsis --x64 *>&1
    if ($LASTEXITCODE -ne 0) {
        $buildLog | Select-Object -Last 25 | ForEach-Object { Write-Output $_ }
        throw "electron-builder 失败（exit $LASTEXITCODE）"
    }
    $installer = Get-ChildItem -Path (Join-Path $desktop 'dist') -Filter '*.exe' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $installer) { throw '未找到安装包产物' }
    $installerSize = [math]::Round($installer.Length / 1MB, 1)
    Write-Output ''
    Write-Output '=== 构建完成 ==='
    Write-Output "安装包：$($installer.FullName)（$installerSize MB）"
    Write-Output '注意：未签名，安装时 Windows SmartScreen 会提示，选择"更多信息 → 仍要运行"。'
} finally {
    Pop-Location
}