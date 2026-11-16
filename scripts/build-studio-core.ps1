# 桌面内核一键打包（P0）
#
#   .\scripts\build-studio-core.ps1 [-Python <python.exe>]
#
# 产物：
#   dist-studio-core\synapforge-core\synapforge-core.exe   （ONEDIR，无控制台）
#
# 说明：--noconsole 的 exe 没有 stdout；内核启动时会自己把日志写到
# `--log-file` 指定的文件（桌面壳会传）。手动试跑：
#   .\dist-studio-core\synapforge-core\synapforge-core.exe --no-browser --log-file .\core.log
#   然后看 %USERPROFILE%\.synapforge\studio.json

param(
    [string]$Python = ''
)

# 与 build-desktop.ps1 同样的理由：PS 5.1 会把原生程序的 stderr 当成终止性错误，
# 这里用 Continue + 显式检查 $LASTEXITCODE。
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot | Split-Path -Parent

if (-not $Python) {
    $candidates = @()
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    foreach ($item in $candidates) {
        if ($item -and (Test-Path $item)) {
            & $item -c "import PyInstaller" 2>$null
            if ($LASTEXITCODE -eq 0) { $Python = $item; break }
        }
    }
}
if (-not $Python) {
    throw '找不到带 PyInstaller 的 Python。先 `python -m pip install pyinstaller`，再用 -Python 指定解释器。'
}

Write-Output "=== Python：$Python ==="
& $Python -c "import sys, PyInstaller, pydantic; print(sys.version.split()[0], 'pyinstaller', PyInstaller.__version__, 'pydantic', pydantic.VERSION)"
if ($LASTEXITCODE -ne 0) { throw '依赖不全：需要 PyInstaller 与 pydantic' }

$dist = Join-Path $root 'dist-studio-core'
$work = Join-Path $root 'dist-studio-core-build'
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist -ErrorAction Stop }

$env:PYTHONPATH = "$root;$root\apps\local;$root\apps\api"
Write-Output '=== PyInstaller（ONEDIR / 无控制台）==='
$log = & $Python -X utf8 -m PyInstaller --noconfirm --clean `
    --distpath $dist --workpath $work `
    (Join-Path $PSScriptRoot 'studio-core.spec') *>&1
$code = $LASTEXITCODE
if ($code -ne 0) {
    $log | Select-Object -Last 25 | ForEach-Object { Write-Output $_ }
    throw "PyInstaller 失败（exit $code）"
}

$exe = Join-Path $dist 'synapforge-core\synapforge-core.exe'
if (-not (Test-Path $exe)) { throw "打包失败：$exe 不存在" }
$size = [math]::Round((Get-ChildItem -Recurse $dist | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Output "内核已打包：$exe（ONEDIR 合计 $size MB）"
