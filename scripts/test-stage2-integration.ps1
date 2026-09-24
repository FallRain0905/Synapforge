param(
    [string]$MinioEndpoint = $env:STAGE2_MINIO_ENDPOINT,
    [string]$PostgresDsn = $env:STAGE2_POSTGRES_DSN
)

$ErrorActionPreference = "Stop"
$pythonCandidates = @(
    $env:STAGE2_PYTHON,
    (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"),
    (Join-Path (Split-Path $PSScriptRoot -Parent) "..\C题工作区\.venv\Scripts\python.exe")
)
$python = $pythonCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $python) {
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($systemPython) {
        $python = $systemPython.Source
    }
}
if (-not $python) {
    throw "No Python interpreter found. Set STAGE2_PYTHON or install the project environment."
}

if ($MinioEndpoint) {
    $env:STAGE2_MINIO_ENDPOINT = $MinioEndpoint
}
if ($PostgresDsn) {
    $env:STAGE2_POSTGRES_DSN = $PostgresDsn
}

$env:PYTHONPATH = "$(Resolve-Path (Join-Path $PSScriptRoot '..\apps\api'));$(Resolve-Path (Join-Path $PSScriptRoot '..\apps\api\vendor'))"
& $python -m unittest discover -s (Join-Path $PSScriptRoot '..\apps\api') -p 'test_stage2_integration.py' -v
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
