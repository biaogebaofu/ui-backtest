$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectPath
$venvPython = Join-Path $projectPath '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    & $venvPython -X utf8 ui.py
} else {
    & python -X utf8 ui.py
}
exit $LASTEXITCODE
