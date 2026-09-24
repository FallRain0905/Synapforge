param(
    [ValidateSet('start', 'stop', 'status', 'provision')]
    [string]$Action = 'start',
    [string]$AdminPassword = $(if ($env:STAGE4_ADMIN_PASSWORD) { $env:STAGE4_ADMIN_PASSWORD } else { 'platform-dev-only' }),
    [string]$RuntimePassword = $(if ($env:STAGE4_RUNTIME_PASSWORD) { $env:STAGE4_RUNTIME_PASSWORD } else { 'app-runtime-dev-only' })
)

# User-mode local infrastructure for integration acceptance (P4-04-RUN-PROD).
# No administrator privileges required: portable PostgreSQL 16 and MinIO run
# as regular user processes on high ports.
#
# PostgreSQL : 127.0.0.1:54329  (admin role 'platform', runtime role 'app_runtime')
# MinIO      : 127.0.0.1:9100   (platform / platform-dev-only), console 9101
#
# The project path contains non-ASCII characters that PostgreSQL tools cannot
# re-exec through, so everything is addressed through the 8.3 short path.

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot | Split-Path -Parent
$infra = Join-Path $root '.infra'
$shortInfra = (New-Object -ComObject Scripting.FileSystemObject).GetFolder($infra).ShortPath
$pgBin = Join-Path $shortInfra 'pg\pgsql\bin'
$pgData = Join-Path $shortInfra 'pgdata'
$pgLog = Join-Path $shortInfra 'pg.log'
$minioExe = Join-Path $shortInfra 'minio.exe'
$minioData = Join-Path $shortInfra 'minio-data'
$hbaPath = Join-Path $pgData 'pg_hba.conf'

$TrustLine = 'host    all             all             127.0.0.1/32            trust'

function Test-Postgres {
    try {
        $result = & (Join-Path $pgBin 'pg_isready.exe') -h 127.0.0.1 -p 54329 -U platform 2>&1
        return "$result" -match 'accepting connections'
    } catch { return $false }
}

function Invoke-Psql {
    param([string]$Sql, [switch]$IgnoreErrors)
    # -w never prompts for a password: a prompt would hang the script forever.
    $output = & (Join-Path $pgBin 'psql.exe') -w -h 127.0.0.1 -p 54329 -U platform -d postgres -v ON_ERROR_STOP=1 -At -c $Sql 2>&1
    if ($LASTEXITCODE -ne 0) {
        if ($IgnoreErrors) { return $false }
        throw "psql failed: $output"
    }
    return $true
}

function Enable-TrustBootstrap {
    $content = Get-Content -Path $hbaPath -Raw
    if ($content -notmatch [regex]::Escape($TrustLine)) {
        # Trust must be matched before the scram rule (first match wins).
        $content = $content -replace '(?m)^(host\s+all\s+all\s+127\.0\.0\.1/32\s+scram-sha-256)', ($TrustLine + "`r`n" + '$1')
        Set-Content -Path $hbaPath -Value $content -NoNewline -Encoding ascii
        $script:bootstrapTrust = $true
    }
    & (Join-Path $pgBin 'pg_ctl.exe') -D $pgData reload | Out-Null
    Start-Sleep -Seconds 1
}

function Disable-TrustBootstrap {
    if (-not $script:bootstrapTrust) { return }
    $content = Get-Content -Path $hbaPath -Raw
    $content = $content -replace [regex]::Escape($TrustLine + "`r`n"), ''
    Set-Content -Path $hbaPath -Value $content -NoNewline -Encoding ascii
    & (Join-Path $pgBin 'pg_ctl.exe') -D $pgData reload | Out-Null
    Start-Sleep -Seconds 1
}

function Test-PasswordLogin {
    param([string]$Role, [string]$Password)
    $env:PGPASSWORD = $Password
    try {
        $output = & (Join-Path $pgBin 'psql.exe') -w -h 127.0.0.1 -p 54329 -U $Role -d postgres -At -c 'SELECT current_user' 2>&1
        return ($LASTEXITCODE -eq 0) -and ("$output" -match $Role)
    } catch { return $false }
    finally { Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue }
}

switch ($Action) {
    'start' {
        if (-not (Test-Postgres)) {
            & (Join-Path $pgBin 'pg_ctl.exe') -D $pgData -o "-p 54329 -h 127.0.0.1" -l $pgLog start
        }
        else { Write-Output 'PostgreSQL already running on 54329' }
        $minioUp = Test-NetConnection -ComputerName 127.0.0.1 -Port 9100 -InformationLevel Quiet -WarningAction SilentlyContinue
        if (-not $minioUp) {
            $env:MINIO_ROOT_USER = 'platform'
            $env:MINIO_ROOT_PASSWORD = 'platform-dev-only'
            Start-Process -FilePath $minioExe -ArgumentList @('server', $minioData, '--address', '127.0.0.1:9100', '--console-address', '127.0.0.1:9101') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $shortInfra 'minio.log') -RedirectStandardError (Join-Path $shortInfra 'minio.err.log')
            Start-Sleep -Seconds 3
        }
        else { Write-Output 'MinIO already running on 9100' }
        Write-Output "PostgreSQL: $(Test-Postgres)"
        Write-Output "MinIO health: $((Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:9100/minio/health/live' -TimeoutSec 5).StatusCode)"
    }
    'provision' {
        # Password bootstrap: set role secrets over a temporary trust window,
        # then restore scram-sha-256 authentication for TCP connections.
        if (-not (Test-Postgres)) { throw 'PostgreSQL is not running; run start first.' }
        $script:bootstrapTrust = $false
        $hasRuntimeRole = $false
        Enable-TrustBootstrap
        try {
            Invoke-Psql "ALTER ROLE platform PASSWORD '$AdminPassword'" | Out-Null
            Write-Output 'admin role password set: platform'
            # Capture the boolean before any pipeline: piping a function into
            # Out-Null discards the return value and makes the test always false.
            # The role lookup must run inside the trust window.
            $hasRuntimeRole = Invoke-Psql "SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime'"
            if ($hasRuntimeRole) {
                Invoke-Psql "ALTER ROLE app_runtime PASSWORD '$RuntimePassword'" | Out-Null
                Invoke-Psql 'ALTER ROLE app_runtime NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE' | Out-Null
                Write-Output 'runtime role password set: app_runtime'
            }
            else {
                Write-Output 'app_runtime not present yet: apply migrations 001-014 first, then re-run provision'
            }
        }
        finally {
            Disable-TrustBootstrap
        }
        Write-Output "platform password login: $(Test-PasswordLogin -Role 'platform' -Password $AdminPassword)"
        if ($hasRuntimeRole) {
            Write-Output "app_runtime password login: $(Test-PasswordLogin -Role 'app_runtime' -Password $RuntimePassword)"
        }
    }
    'stop' {
        if (Test-Postgres) {
            & (Join-Path $pgBin 'pg_ctl.exe') -D $pgData stop -m fast
        }
        Get-Process -Name minio -ErrorAction SilentlyContinue | Stop-Process -Force
        Write-Output 'stopped'
    }
    'status' {
        Write-Output "PostgreSQL accepting connections: $(Test-Postgres)"
        try {
            $code = (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:9100/minio/health/live' -TimeoutSec 5).StatusCode
            Write-Output "MinIO health: $code"
        } catch { Write-Output 'MinIO health: down' }
    }
}
