# DP-1-06 验收：清理失败安装 → 正确重装 → 验证随包内核 → 卸载并核对残留
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File verify-desktop-install.ps1
$ErrorActionPreference = 'Continue'

$repo = Split-Path $PSScriptRoot -Parent
$installer = Join-Path $repo 'apps\desktop\dist\Math Agent Platform Setup 0.1.0.exe'
$target = Join-Path $env:TEMP 'map-install-test'

Write-Output '=== 1/6 清理上一次失败的安装记录 ==='
# 只清理**指向本次临时目标目录**的残留项。
# 早期版本按 DisplayName 匹配删除，会把用户自己正常安装的那份卸载入口也删掉（不可逆），
# 因此这里按 InstallLocation / UninstallString 精确限定在 $env:TEMP 下。
$uninstallRoot = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
$tempRoot = ([string]$env:TEMP).TrimEnd('\')
$stale = Get-ChildItem $uninstallRoot -ErrorAction SilentlyContinue | Where-Object {
    $entry = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
    if (-not $entry -or $entry.DisplayName -notlike '*Math Agent Platform*') { return $false }
    $location = ([string]$entry.InstallLocation).TrimEnd('\')
    if ($location) { return $location -ieq $target.TrimEnd('\') }
    return ([string]$entry.UninstallString) -like "*$tempRoot*"
}
foreach ($key in $stale) {
    Write-Output ("  删除本次临时安装的残留项：" + (Get-ItemProperty $key.PSPath).DisplayName)
    Remove-Item $key.PSPath -Recurse -Force -ErrorAction SilentlyContinue
}
if (-not $stale) { Write-Output '  （无本次临时残留）' }
$others = Get-ChildItem $uninstallRoot -ErrorAction SilentlyContinue | Where-Object {
    (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DisplayName -like '*Math Agent Platform*'
}
foreach ($key in $others) {
    Write-Output ("  注意：本机还有另一份已安装副本，本次不动它：" + (Get-ItemProperty $key.PSPath).UninstallString)
}

Write-Output '=== 2/6 静默安装到 Windows 绝对路径 ==='
if (Test-Path $target) { Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue }
# /D 必须是最后一个参数，且用反斜杠绝对路径、不加引号（NSIS 的硬性要求）
$proc = Start-Process -FilePath $installer -ArgumentList '/S', "/D=$target" -Wait -PassThru
Write-Output ("  安装退出码：" + $proc.ExitCode)

Write-Output '=== 3/6 检查安装内容 ==='
if (-not (Test-Path $target)) { Write-Output '  安装目录不存在，安装失败'; exit 1 }
$appExe = Get-ChildItem $target -Filter '*.exe' | Select-Object -First 3
$appExe | ForEach-Object { Write-Output ("  可执行：" + $_.Name + "（" + [math]::Round($_.Length / 1MB, 1) + " MB）") }
$bundledKernel = Join-Path $target 'resources\sidecar\math-agent-sidecar.exe'
if (Test-Path $bundledKernel) {
    Write-Output ("  随包内核：resources\sidecar\math-agent-sidecar.exe（" + [math]::Round((Get-Item $bundledKernel).Length / 1MB, 1) + " MB）")
} else {
    Write-Output '  ⚠ 未找到随包内核：extraResources 没打进去'
}

Write-Output '=== 4/6 启动桌面端（内核应被拉起）==='
$stateDir = Join-Path $env:TEMP 'map-install-state'
if (Test-Path $stateDir) { Remove-Item -Recurse -Force $stateDir -ErrorAction SilentlyContinue }
$shellExe = Get-ChildItem $target -Filter '*.exe' | Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
$env:MAP_STATE_DIR = $stateDir
$app = Start-Process -FilePath $shellExe.FullName -PassThru
$deadline = (Get-Date).AddSeconds(30)
$infoPath = Join-Path $stateDir 'sidecar.json'
while ((Get-Date) -lt $deadline -and -not (Test-Path $infoPath)) { Start-Sleep -Milliseconds 800 }
if (Test-Path $infoPath) {
    $info = Get-Content $infoPath -Raw | ConvertFrom-Json
    Write-Output ("  ✓ 内核已由安装版拉起：port=" + $info.port + " contract=" + $info.contract)
    try {
        $health = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $info.port + "/health") -TimeoutSec 10
        Write-Output ("  ✓ /health：" + $health.status + "（version " + $health.version + "）")
    } catch { Write-Output ("   /health 失败：" + $_.Exception.Message) }
} else {
    Write-Output '   30 秒内未见内核启动信息'
}

Write-Output '=== 5/6 关闭应用 ==='
if (-not $app.HasExited) { Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue }
Get-Process -Name 'math-agent-sidecar' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process -Name 'Math Agent Platform' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Write-Output '  已关闭壳与内核'

Write-Output '=== 6/6 卸载并核对残留 ==='
$uninstaller = Get-ChildItem $target -Filter 'Uninstall*.exe' | Select-Object -First 1
if ($uninstaller) {
    $un = Start-Process -FilePath $uninstaller.FullName -ArgumentList '/S','/currentuser' -Wait -PassThru
    Write-Output ("  卸载退出码：" + $un.ExitCode)
    Start-Sleep -Seconds 5
} else {
    Write-Output '  ⚠ 未找到卸载程序'
}
$residue = if (Test-Path $target) { (Get-ChildItem $target -ErrorAction SilentlyContinue | Measure-Object).Count } else { 0 }
Write-Output ("  安装目录残留项：" + $residue)
$stillRegistered = Get-ChildItem $uninstallRoot -ErrorAction SilentlyContinue | Where-Object {
    (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DisplayName -like '*Math Agent Platform*'
}
Write-Output ("  注册表卸载项残留：" + (@($stillRegistered).Count))
$cred = cmdkey /list 2>$null | Select-String 'MathAgentPlatform'
Write-Output ("  凭据残留（MathAgentPlatform/*）：" + (@($cred).Count) + " 条")
Write-Output ''
Write-Output '验收要点：安装内容含随包内核、安装版能拉起内核并应答 /health、卸载后目录与注册表无残留。'