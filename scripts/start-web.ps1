$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$web = Join-Path $root "apps\web"
Push-Location $web
try {
  if (-not (Test-Path "node_modules")) { npm install }
  npm run dev -- --hostname 127.0.0.1 --port 3000
} finally {
  Pop-Location
}

