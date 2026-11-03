param([string]$Python)
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if(-not $Python){$Python=Join-Path $projectRoot '.tools\python\python.exe'}
if(-not(Test-Path -LiteralPath $Python)){throw 'Use this project Python or pass -Python.'}
Push-Location -LiteralPath $projectRoot
try{
    & $Python -m compileall -q runtime tests
    if($LASTEXITCODE -ne 0){throw 'Python syntax check failed.'}
    & $Python tools/run-runtime-tests.py
    if($LASTEXITCODE -ne 0){throw 'Runtime regressions failed.'}
}finally{Pop-Location}
