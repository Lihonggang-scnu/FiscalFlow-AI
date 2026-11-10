param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
$PythonPath = $env:TAXHARNESS_PYTHON
if (-not $PythonPath) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw 'Python not found; set TAXHARNESS_PYTHON.' }
    $PythonPath = $pythonCommand.Source
}
& $PythonPath -m taxharness.cli @Arguments
exit $LASTEXITCODE
