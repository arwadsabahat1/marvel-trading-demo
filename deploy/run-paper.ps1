param([switch]$Once)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create the documented Python 3.12 environment first.' }
$boundedArguments = @()
if ($Once) { $boundedArguments = @('--max-ticks', '1') }
Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath -m marvel_ops worker --allow-network @boundedArguments
    $resultCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $resultCode
