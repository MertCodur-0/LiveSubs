# Builds dist\LiveSubs\ (LiveSubs.exe + runtime) and dist\LiveSubs-<version>-win64.zip.
#
# LiveSubs.exe is the signed pythonw.exe of the official embeddable Python, renamed: Windows Smart
# App Control blocks unsigned executables without reputation, which every PyInstaller build is.
# The app and its runtime dependencies are installed next to it; scripts\embedded\sitecustomize.py
# starts the app on a double-click.
#
# Usage (from the repository root):  .\scripts\build_exe.ps1
# Native tools log to stderr, which "Stop" would turn into errors on Windows PowerShell 5.1:
# failures are detected through $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Create the venv first: py -3.13 -m venv .venv" }

# 1. The embeddable Python of exactly the venv's version (cached in build\).
$pyVersion = & $python -c "import platform; print(platform.python_version())"
$pyTag = & $python -c "import sys; print(f'python{sys.version_info.major}{sys.version_info.minor}')"
$build = Join-Path $root "build"
New-Item -ItemType Directory -Force $build | Out-Null
$embedZip = Join-Path $build "python-$pyVersion-embed-amd64.zip"
if (-not (Test-Path $embedZip)) {
    $url = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $embedZip -UseBasicParsing
}

# 2. A fresh dist folder; the exe must carry a valid Python Software Foundation signature.
$dist = Join-Path $root "dist\LiveSubs"
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
Expand-Archive -Path $embedZip -DestinationPath $dist
$signature = Get-AuthenticodeSignature (Join-Path $dist "pythonw.exe")
if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
    throw "pythonw.exe is not validly signed by the Python Software Foundation: $($signature.Status)"
}
Move-Item (Join-Path $dist "pythonw.exe") (Join-Path $dist "LiveSubs.exe")
Remove-Item (Join-Path $dist "python.exe")

# 3. Import paths: the standard library zip, the app folder and site-packages; "import site" runs
#    site-packages' .pth files and sitecustomize (the launcher).
Set-Content -Encoding ascii (Join-Path $dist "$pyTag._pth") "$pyTag.zip`r`n.`r`nLib\site-packages`r`n`r`nimport site"

# 4. The app and its runtime dependencies (no dev tools), as prebuilt wheels.
$sitePackages = Join-Path $dist "Lib\site-packages"
& $python -m pip install --quiet --disable-pip-version-check --no-warn-script-location `
    --only-binary=:all: --target $sitePackages $root
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Remove-Item -Recurse -Force (Join-Path $sitePackages "bin") -ErrorAction SilentlyContinue
Copy-Item (Join-Path $PSScriptRoot "embedded\sitecustomize.py") $dist

# 4b. Qt parts the app never loads (it uses QtCore, QtGui, QtWidgets and SVG): QML/Quick,
#     Designer, the developer tools, Qt's own translations and the software OpenGL renderer.
$qt = Join-Path $sitePackages "PySide6"
foreach ($dir in "qml", "translations", "resources", "metatypes", "typesystems", "include", "glue",
                 "plugins\designer", "plugins\qmltooling", "plugins\sqldrivers") {
    Remove-Item -Recurse -Force (Join-Path $qt $dir) -ErrorAction SilentlyContinue
}
$unused = "^Qt6?(Qml|Quick|Designer|Labs|Sql|Test|Help|OpenGL|Concurrent|DBus|PrintSupport|Xml|UiTools|Pdf|ShaderTools)"
Get-ChildItem $qt -File | Where-Object {
    $_.Extension -eq ".exe" -or $_.Name -eq "opengl32sw.dll" -or $_.Name -match $unused
} | Remove-Item -Force

# 4c. Smoke test of the packaged runtime (a windowless exe: the result goes to a report file).
$report = Join-Path $build "smoke_test.txt"
Remove-Item $report -ErrorAction SilentlyContinue
Start-Process -Wait -FilePath (Join-Path $dist "LiveSubs.exe") `
    -ArgumentList "`"$(Join-Path $PSScriptRoot 'embedded\smoke_test.py')`"", "`"$report`""
$result = if (Test-Path $report) { Get-Content -Raw $report } else { "no report written" }
if (-not $result.StartsWith("ok")) { throw "Smoke test failed:`n$result" }

# 5. Release zip (Python's zipfile copes with PySide6's deep paths; Compress-Archive may not).
$version = & $python -c "import livesubs; print(livesubs.__version__)"
$zip = Join-Path $root "dist\LiveSubs-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
& $python -c "import shutil, sys; shutil.make_archive(sys.argv[1][:-4], 'zip', sys.argv[2], 'LiveSubs')" $zip (Join-Path $root "dist")
if ($LASTEXITCODE -ne 0) { throw "zip failed" }
$size = [math]::Round((& $python -c "import os, sys; print(sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(sys.argv[1]) for f in fs))" $dist) / 1MB)
Write-Host "Built $dist ($size MB, LiveSubs.exe signed by: $($signature.SignerCertificate.Subject.Split(',')[0])) and $zip"
