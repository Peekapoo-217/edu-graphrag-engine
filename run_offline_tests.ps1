param(
    [string]$PythonExecutable = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv-data-scrawler\Scripts\python.exe"),
    [string]$StartDirectory = "tests",
    [string]$Pattern = "test*.py"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Data Scrawler Python executable not found: $PythonExecutable"
}

$existingPythonPath = $env:PYTHONPATH
$requiredPaths = @($repositoryRoot, $PSScriptRoot, (Join-Path $PSScriptRoot "tests"))
if (-not [string]::IsNullOrWhiteSpace($existingPythonPath)) {
    $requiredPaths += $existingPythonPath.Split([IO.Path]::PathSeparator)
}
$env:PYTHONPATH = (($requiredPaths | Where-Object { $_ } | Select-Object -Unique) -join [IO.Path]::PathSeparator)

Push-Location $PSScriptRoot
$previousErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    $offlineRunner = Join-Path $repositoryRoot "test_support\run_unittest_offline.py"
    & $PythonExecutable $offlineRunner --start-dir $StartDirectory --pattern $Pattern --verbosity 2
    $testExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
    Pop-Location
}

exit $testExitCode
