param(
    [string]$PythonPath = "artifacts/full_acceptance_20261026/phase/clean_venv/Scripts/python.exe",
    [string]$Database = "artifacts/enterprise_delivery_20261027/enterprise.sqlite3",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8011
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
function Resolve-ProjectPath {
    param([string]$Value)
    if ([IO.Path]::IsPathRooted($Value)) { return [IO.Path]::GetFullPath($Value) }
    return [IO.Path]::GetFullPath((Join-Path $ProjectRoot $Value))
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw "Python not found; pass -PythonPath." }
    $PythonPath = $pythonCommand.Source
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$Database = Resolve-ProjectPath $Database
$ArtifactDir = Join-Path $ProjectRoot "artifacts/enterprise_delivery_20261027"
$ArtifactRoot = [IO.Path]::GetFullPath($ArtifactDir).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
$DatabaseFullPath = [IO.Path]::GetFullPath($Database)
if (-not $DatabaseFullPath.StartsWith($ArtifactRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Enterprise database must stay under the isolated output directory: $ArtifactRoot"
}
$Database = $DatabaseFullPath
$PidFile = Join-Path $ArtifactDir "enterprise.pid"
$StdoutLog = Join-Path $ArtifactDir "enterprise.stdout.log"
$StderrLog = Join-Path $ArtifactDir "enterprise.stderr.log"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Database),$ArtifactDir | Out-Null
if (Test-Path -LiteralPath $PidFile) {
    $oldRecord = Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json
    $oldPid = [int]$oldRecord.pid
    if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) { throw "Enterprise UI is already running; PID=$oldPid." }
    Remove-Item -LiteralPath $PidFile -Force
}
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$env:TAXHARNESS_ENTERPRISE_DATABASE = $Database
$env:TAXHARNESS_AGENT_STORE = Join-Path $ProjectRoot "artifacts/agent/agent.sqlite3"
$env:TAXHARNESS_ENTERPRISE_HOST = $HostAddress
$env:TAXHARNESS_ENTERPRISE_PORT = [string]$Port
if (-not $env:TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED) { $env:TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED = "false" }
if (-not $env:TAXHARNESS_LIVE_MODEL_ENABLED) { $env:TAXHARNESS_LIVE_MODEL_ENABLED = "false" }
$AppPath = Join-Path $ProjectRoot "scripts/enterprise_app.py"
$QuotedAppPath = '"' + $AppPath + '"'
$process = Start-Process -FilePath $PythonPath -ArgumentList @($QuotedAppPath) -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog -PassThru
$record = [ordered]@{
    pid = $process.Id
    started_at = $process.StartTime.ToUniversalTime().ToString("o")
    script = $AppPath
    port = $Port
    host = $HostAddress
    python = $PythonPath
    database = $Database
}
$record | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding utf8
$ready = $false
for ($attempt = 0; $attempt -lt 15; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
        if ($health.status -eq "ok") { $ready = $true; break }
    } catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $ready) {
    $stderrTail = if (Test-Path -LiteralPath $StderrLog) { Get-Content -LiteralPath $StderrLog -Tail 30 } else { @() }
    throw "Enterprise UI did not become ready. See $StderrLog. $($stderrTail -join [Environment]::NewLine)"
}
Write-Output "Enterprise UI started: PID=$($process.Id) URL=http://$HostAddress`:$Port database=$Database"
Write-Output "Logs: $StdoutLog ; $StderrLog"
