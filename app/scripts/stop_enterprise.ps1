param(
    [string]$PidFile = "artifacts/enterprise_delivery_20261027/enterprise.pid"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not [IO.Path]::IsPathRooted($PidFile)) { $PidFile = Join-Path $ProjectRoot $PidFile }
if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Output "Enterprise PID file not found; no service owned by this script."
    exit 0
}
$record = Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json
$targetPid = [int]$record.pid
$process = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
if ($process) {
    $expectedStart = ([DateTimeOffset]$record.started_at).UtcDateTime
    $actualStart = $process.StartTime.ToUniversalTime()
    if ([Math]::Abs(($actualStart - $expectedStart).TotalSeconds) -gt 10) {
        throw "PID $targetPid start time does not match the owned record; refusing to stop."
    }
    if (@("python", "python3") -notcontains $process.ProcessName) {
        throw "PID $targetPid is not a Python server process; refusing to stop."
    }
    Stop-Process -Id $targetPid -ErrorAction Stop
    Wait-Process -Id $targetPid -Timeout 15 -ErrorAction SilentlyContinue
    if (Get-Process -Id $targetPid -ErrorAction SilentlyContinue) { Stop-Process -Id $targetPid -Force -ErrorAction Stop }
    Write-Output "Enterprise UI stopped: PID=$targetPid"
} else {
    Write-Output "Enterprise process $targetPid was already stopped."
}
Remove-Item -LiteralPath $PidFile -Force
