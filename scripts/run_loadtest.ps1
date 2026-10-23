<#
.SYNOPSIS
  Load test against the DEDICATED lamsa_load database with production-like settings.

.DESCRIPTION
  1. Re-seeds lamsa_load (same starting data for every run).
  2. Starts the API as 4 single-worker Uvicorn processes on ports 8001-8004 (Locust spreads its
     users over them, like Nginx over upstreams; on Windows, one `uvicorn --workers 4` hands
     nearly every connection to the same process) and the separate worker process, with
     DB_NAME=lamsa_load, PUSH_BACKEND=log, RUN_WORKER_IN_APP=false, APP_ENV=production.
     TRUST_PROXY_HEADERS=true is set HERE ONLY, so Locust can send one fake client IP per
     simulated user (never enable it in production unless a trusted proxy overwrites the header).
  3. Runs Locust headless (1 master + 4 worker processes) in four phases:
       browse     AnonymousBrowser / LoggedInBrowser / Shopper at 50, 200, 500, 1000 users
       flash      300 FlashBuyer at once for a product with stock 50, then verified in the database
       login      400 LoginBurst users logging in as fast as they can
       broadcast  200 browsing users while CampaignAdmin sends one campaign to all 10,000 users
  4. Writes loadtest/results/cache-<on|off>/ (Locust CSVs, API log, monitor CSV, summary.md).

.EXAMPLE
  .\scripts\run_loadtest.ps1 -Cache on
  .\scripts\run_loadtest.ps1 -Cache off -StageSeconds 60
#>
param(
    [ValidateSet("on", "off")] [string] $Cache = "on",
    [int] $StageSeconds = 90,
    [int] $ApiWorkers = 4,
    [int] $LocustWorkers = 4,
    [int] $Port = 8001
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root "venv\Scripts\python.exe"
$LoadPy = Join-Path $Root ".venv-load\Scripts\python.exe"
if (-not (Test-Path $LoadPy)) { throw "Create the load-test venv first (see README, Load testing)." }

# ------------------------------------------------------------------ isolation + settings
$env:DB_NAME = "lamsa_load"
if (-not $env:DB_NAME.EndsWith("_load")) { throw "DB_NAME must end with _load" }
$env:APP_ENV = "production"
$env:DEBUG = "false"
$env:GOOGLE_DEV_MODE = "false"
$env:PUSH_BACKEND = "log"
$env:RUN_WORKER_IN_APP = "false"
$env:TRUST_PROXY_HEADERS = "true"      # load test only: Locust sends fake X-Forwarded-For per user
$env:MEDIA_ROOT = "./media-load"
$env:MEDIA_URL = "http://127.0.0.1:$Port/media"
$env:CACHE_ENABLED = if ($Cache -eq "on") { "true" } else { "false" }
$env:PASSWORD_HASH_CONCURRENCY = [string][Math]::Max(1, [Math]::Floor([Environment]::ProcessorCount / $ApiWorkers))
$env:LOAD_STAGE_SECONDS = [string]$StageSeconds
$env:PYTHONPATH = $Root
$env:PYTHONUNBUFFERED = "1"   # logs must survive taskkill at the end
$Host_ = "http://127.0.0.1:$Port"
$Ports = 0..($ApiWorkers - 1) | ForEach-Object { $Port + $_ }
$env:LOAD_API_PORTS = ($Ports -join ",")
$Out = Join-Path $Root "loadtest\results\cache-$Cache"
New-Item -ItemType Directory -Force $Out | Out-Null
Get-ChildItem $Out -File | Remove-Item -Force

Write-Host "== Cache $Cache, $ApiWorkers API workers, hash concurrency $($env:PASSWORD_HASH_CONCURRENCY) per worker"

# Never measure a stale server: another process on the port would silently take requests.
foreach ($p in $Ports) {
    if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $p is already in use (an old API instance?). Stop it first."
    }
}

# ------------------------------------------------------------------------------ fresh data
& $Py -m scripts.seed_load_data --reset
if ($LASTEXITCODE -ne 0) { throw "seeding failed" }
if ($Cache -eq "on") { docker compose exec -T valkey valkey-cli FLUSHALL | Out-Null }

# ------------------------------------------------------------------------ API + worker
$apis = @()
foreach ($p in $Ports) {
    $apis += Start-Process -FilePath $Py -PassThru -NoNewWindow `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$p",
                      "--no-access-log", "--log-config", "loadtest/logging.json", "--timeout-keep-alive", "15" `
        -RedirectStandardOutput "$Out\api-$p.log" -RedirectStandardError "$Out\api-$p.err.log"
}
$worker = Start-Process -FilePath $Py -PassThru -NoNewWindow -ArgumentList "-m", "app.workers.run" `
    -RedirectStandardOutput "$Out\worker.log" -RedirectStandardError "$Out\worker.err.log"

function Stop-Tree([System.Diagnostics.Process] $Process) {
    # The whole tree (uvicorn --workers spawns children). Start-Process: taskkill's stderr must
    # not become a PowerShell error when a child has already exited.
    if ($Process -and -not $Process.HasExited) {
        Start-Process -FilePath "taskkill.exe" -ArgumentList "/T", "/F", "/PID", "$($Process.Id)" -NoNewWindow -Wait `
            -RedirectStandardOutput "$Out\taskkill.log" -RedirectStandardError "$Out\taskkill.err.log"
    }
}

$monitor = $null
try {
    $deadline = (Get-Date).AddSeconds(60)
    foreach ($p in $Ports) {
        do {
            Start-Sleep -Milliseconds 500
            try { $ready = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$p/health" -TimeoutSec 2).StatusCode -eq 200 } catch { $ready = $false }
        } until ($ready -or (Get-Date) -gt $deadline)
        if (-not $ready) { throw "API on port $p did not start (see $Out\api-$p.err.log)" }
    }

    $monitor = Start-Process -FilePath $LoadPy -PassThru -NoNewWindow `
        -ArgumentList "loadtest/monitor.py", "--out", "$Out\monitor.csv", "--api-pids", (($apis | ForEach-Object { $_.Id }) -join ","), "--worker-pid", "$($worker.Id)"

    function Invoke-Locust([string] $Phase, [string[]] $Arguments) {
        Write-Host "== Phase $Phase"
        $common = @("-m", "locust", "--host", $Host_, "--headless", "--only-summary")
        $workers = @()
        for ($i = 0; $i -lt $LocustWorkers; $i++) {
            $workers += Start-Process -FilePath $LoadPy -PassThru -NoNewWindow `
                -ArgumentList (@("-m", "locust", "--worker", "--master-host", "127.0.0.1") + $Arguments) `
                -RedirectStandardOutput "$Out\$Phase-worker$i.log" -RedirectStandardError "$Out\$Phase-worker$i.err.log"
        }
        try {
            # Start-Process + -Wait: Locust logs to stderr, which PowerShell 5.1 would turn into errors.
            $master = Start-Process -FilePath $LoadPy -PassThru -NoNewWindow -Wait `
                -ArgumentList ($common + @("--master", "--expect-workers", "$LocustWorkers", "--csv", "$Out\$Phase", "--csv-full-history") + $Arguments) `
                -RedirectStandardOutput "$Out\$Phase-locust.log" -RedirectStandardError "$Out\$Phase-locust.err.log"
            Write-Host "   locust exit code $($master.ExitCode) (1 = some requests failed, see the summary)"
        }
        finally {
            foreach ($w in $workers) { Stop-Tree $w }
        }
    }

    Invoke-Locust "browse" @("-f", "loadtest/locustfile.py,loadtest/shape_stages.py", "AnonymousBrowser", "LoggedInBrowser", "Shopper")

    & $Py -m scripts.seed_load_data --flash-stock 50 | Out-Null
    Invoke-Locust "flash" @("-f", "loadtest/locustfile.py", "-u", "300", "-r", "300", "-t", "40s", "FlashBuyer")
    & $Py -m scripts.seed_load_data --verify-flash 50 | Tee-Object -FilePath "$Out\flash.txt"

    Invoke-Locust "login" @("-f", "loadtest/locustfile.py", "-u", "400", "-r", "400", "-t", "30s", "LoginBurst")

    Invoke-Locust "broadcast" @("-f", "loadtest/locustfile.py", "-u", "200", "-r", "50", "-t", "75s", "AnonymousBrowser", "CampaignAdmin")
    Start-Sleep -Seconds 5
    & $Py -m scripts.seed_load_data --broadcast-report | Tee-Object -FilePath "$Out\broadcast.txt"
}
finally {
    New-Item -ItemType File -Force "$Out\monitor.csv.stop" | Out-Null
    Start-Sleep -Seconds 2
    Stop-Tree $monitor
    Stop-Tree $worker
    foreach ($a in $apis) { Stop-Tree $a }
}

Get-Content "$Out\api-*.log" | Set-Content -Encoding utf8 "$Out\api.log"   # one log for the report
& $LoadPy loadtest/report.py $Out | Out-Null
Write-Host "== Done: $Out\summary.md"
