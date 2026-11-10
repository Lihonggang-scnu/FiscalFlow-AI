param(
    [string]$PythonPath = $env:TAXHARNESS_PYTHON,
    [string]$Database = "artifacts/chainlit/live-business.sqlite3",
    [string]$AgentStore = "artifacts/agent/agent.sqlite3",
    [string]$PreferenceStore = "artifacts/chainlit/preferences.sqlite3",
    [string]$CheckpointStore = "artifacts/chainlit/checkpoints.sqlite3",
    [string]$SourceXlsx = "data/02_示例数据.xlsx",
    [string]$RulesDoc = "data/03_业务规则.docx.txt",
    [int]$Port = 8010,
    [double]$BudgetUsd = 2.0
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
function Resolve-ProjectPath {
    param([string]$Value)
    if ([IO.Path]::IsPathRooted($Value)) { return [IO.Path]::GetFullPath($Value) }
    return [IO.Path]::GetFullPath((Join-Path $ProjectRoot $Value))
}
if (-not $PythonPath) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw "Python not found; pass -PythonPath." }
    $PythonPath = $pythonCommand.Source
}
$PythonPath = (Resolve-Path $PythonPath).Path
$Database = Resolve-ProjectPath $Database
$AgentStore = Resolve-ProjectPath $AgentStore
$PreferenceStore = Resolve-ProjectPath $PreferenceStore
$CheckpointStore = Resolve-ProjectPath $CheckpointStore
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$UiDir = Join-Path $ProjectRoot "artifacts/chainlit"
$PidFile = Join-Path $UiDir "chainlit.pid"
$StdoutLog = Join-Path $UiDir "chainlit.stdout.log"
$StderrLog = Join-Path $UiDir "chainlit.stderr.log"
New-Item -ItemType Directory -Force -Path (Split-Path $Database -Parent),$UiDir | Out-Null
if (Test-Path $PidFile) {
    $oldRecord = Get-Content -Raw $PidFile | ConvertFrom-Json
    $oldPid = [int]$oldRecord.pid
    if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) { throw "Chainlit is already running; PID=$oldPid." }
    Remove-Item -LiteralPath $PidFile -Force
}
if (-not (Test-Path $Database)) {
    if (-not $SourceXlsx -or -not $RulesDoc) { throw "Database is missing; pass -SourceXlsx and -RulesDoc for first start." }
    $SourceXlsx = Resolve-ProjectPath $SourceXlsx
    $RulesDoc = Resolve-ProjectPath $RulesDoc
    & $PythonPath -m taxharness.cli init-db --database $Database --source-xlsx $SourceXlsx --rules-doc $RulesDoc
    if ($LASTEXITCODE -ne 0) { throw "Database initialization failed, exit code=$LASTEXITCODE." }
}
$env:TAXHARNESS_DATABASE = $Database
$env:TAXHARNESS_AGENT_STORE = $AgentStore
$env:TAXHARNESS_PREFERENCE_STORE = $PreferenceStore
$env:TAXHARNESS_CHECKPOINT_STORE = $CheckpointStore
$env:TAXHARNESS_UI_BUDGET_USD = "$BudgetUsd"
if ($BudgetUsd -gt 0) { $env:TAXHARNESS_LIVE_MODEL_ENABLED = "true" }
$AppPath = Join-Path $ProjectRoot "scripts/chainlit_app.py"
$QuotedAppPath = '"' + $AppPath + '"'
$arguments = @(
    "-m", "chainlit", "run", $QuotedAppPath,
    "--headless", "--host", "127.0.0.1", "--port", "$Port"
)
$process = Start-Process -FilePath $PythonPath -ArgumentList $arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog -PassThru
$record = [ordered]@{
    pid = $process.Id
    started_at = $process.StartTime.ToUniversalTime().ToString("o")
    script = $AppPath
    port = $Port
    python = $PythonPath
}
$record | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding utf8
Write-Output "Chainlit started: PID=$($process.Id) URL=http://127.0.0.1:$Port database=$Database ledger=$AgentStore"
