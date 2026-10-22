# Creates a "LiveSubs" desktop shortcut that runs the app from the development venv.
#
# Why not the built exe: Windows 11 Smart App Control blocks unsigned executables without
# reputation, and every PyInstaller build is a new unknown binary. pythonw.exe is signed, so the
# app starts through it (no console window). Code signing for releases is planned (step 18).
#
# Usage (from the repository root):  .\scripts\create_shortcut.ps1
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Create the venv first: py -3.13 -m venv .venv" }

$desktop = [Environment]::GetFolderPath("Desktop")  # follows OneDrive redirection
$path = Join-Path $desktop "LiveSubs.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "-m livesubs"
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = (Join-Path $root "src\livesubs\assets\icon.ico") + ",0"
$shortcut.Description = "LiveSubs - oyun altyazilarini yerelde Turkceye cevirir"
$shortcut.Save()
Write-Host "Created $path"
