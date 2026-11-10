param(
    [string]$PidFile = "artifacts/chainlit/chainlit.pid"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not [IO.Path]::IsPathRooted($PidFile)) { $PidFile = Join-Path $ProjectRoot $PidFile }
if (-not (Test-Path $PidFile)) {
    Write-Output "Chainlit PID file not found; no service owned by this script."
    exit 0
}
$record = Get-Content -Raw $PidFile | ConvertFrom-Json
$targetPid = [int]$record.pid
$process = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
if ($process) {
    $expectedStart = ([DateTimeOffset]$record.started_at).UtcDateTime
    $actualStart = $process.StartTime.ToUniversalTime()
    if ([Math]::Abs(($actualStart - $expectedStart).TotalSeconds) -gt 10) {
        throw "PID $targetPid start time does not match the owned record; refusing to stop."
    }
    if (@("python", "python3", "pwsh", "powershell") -notcontains $process.ProcessName) {
        throw "PID $targetPid is not a supported Chainlit/Python process; refusing to stop."
    }
    Stop-Process -Id $targetPid -ErrorAction Stop
    Wait-Process -Id $targetPid -Timeout 15 -ErrorAction SilentlyContinue
    if (Get-Process -Id $targetPid -ErrorAction SilentlyContinue) { Stop-Process -Id $targetPid -Force -ErrorAction Stop }
    Write-Output "Chainlit stopped: PID=$targetPid"
} else {
    Write-Output "Chainlit process $targetPid was already stopped."
}
Remove-Item -LiteralPath $PidFile -Force
