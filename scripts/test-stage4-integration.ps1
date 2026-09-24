param(
    [string]$MinioEndpoint = $env:STAGE4_MINIO_ENDPOINT,
    [string]$PostgresDsn = $env:STAGE4_POSTGRES_DSN
)

$ErrorActionPreference = "Stop"
$pythonCandidates = @(
    $env:STAGE4_PYTHON,
    (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"),
    (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
)
$python = $pythonCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $python) {
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($systemPython) {
        $python = $systemPython.Source
    }
}
if (-not $python) {
    throw "No Python interpreter found. Set STAGE4_PYTHON or install the project environment."
}

if ($MinioEndpoint) {
    $env:STAGE4_MINIO_ENDPOINT = $MinioEndpoint
}
if ($PostgresDsn) {
    $env:STAGE4_POSTGRES_DSN = $PostgresDsn
}

$apiPath = (Resolve-Path (Join-Path $PSScriptRoot "..\apps\api")).Path
$vendorPath = (Resolve-Path (Join-Path $PSScriptRoot "..\apps\api\vendor")).Path
$env:PYTHONPATH = $apiPath
$bootstrap = @"
import sys
import unittest

# Load the bundled ABI before the vendored cryptography package on Windows.
import _cffi_backend  # noqa: F401
sys.path.insert(0, r'$vendorPath')
sys.path.insert(0, r'$apiPath')
suite = unittest.defaultTestLoader.discover(r'$apiPath', pattern='test_stage4_integration.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
"@
& $python -X utf8 -c $bootstrap
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
