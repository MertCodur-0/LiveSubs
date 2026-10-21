# Builds dist\LiveSubs\LiveSubs.exe (one folder) and a zip for GitHub Releases.
# Usage (from the repository root):  .\scripts\build_exe.ps1
# Native tools log to stderr, which "Stop" would turn into errors on Windows PowerShell 5.1:
# failures are detected through $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Create the venv first: py -3.13 -m venv .venv" }

& $python -m pip install --quiet -e "$root[build]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

& $python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build") `
    (Join-Path $PSScriptRoot "livesubs.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$version = & $python -c "import livesubs; print(livesubs.__version__)"
$app = Join-Path $root "dist\LiveSubs"
$zip = Join-Path $root "dist\LiveSubs-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $app -DestinationPath $zip
$size = [math]::Round((Get-ChildItem $app -Recurse | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Built $app ($size MB) and $zip"
