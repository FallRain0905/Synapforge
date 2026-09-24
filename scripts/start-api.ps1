$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$api = Join-Path $root "apps\api"
$vendor = Join-Path $api "vendor"
$env:PYTHONPATH = "$root;$api;$vendor"
$systemPython = (Get-Command python -ErrorAction SilentlyContinue).Source
$pythonCandidates = @(
  $systemPython,
  (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
)
$python = $null
foreach ($candidate in $pythonCandidates) {
  if ($candidate -and (Test-Path $candidate)) {
    & $candidate -c "import fastapi" *> $null
    if ($LASTEXITCODE -eq 0) {
      $python = $candidate
      break
    }
  }
}
if (-not $python) { throw "没有找到 Python。请安装 Python 3.12+，或配置工作区 Python。" }

Push-Location $api
try {
  & $python -c "import sys; sys.argv=['uvicorn','app.main:app','--reload','--host','127.0.0.1','--port','8000']; from uvicorn.main import main; main()"
} finally {
  Pop-Location
}
