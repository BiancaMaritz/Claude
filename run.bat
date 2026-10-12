@echo off
rem Launch the SDD Generator at http://localhost:8765/
cd /d "%~dp0"

rem Everything you load is kept here, whichever folder this script runs from.
if not defined SDD_DATA_DIR set "SDD_DATA_DIR=C:\Dev\sdd-generator\data\documents"

rem If this folder is a git clone, pick up the latest version first. Failure is not fatal.
if exist .git (
  where git >nul 2>nul && (git pull --ff-only || echo Could not update; continuing with the version on disk.)
)

if not exist .venv\Scripts\python.exe (
  python -m venv .venv || goto :err
)
.venv\Scripts\python -m pip install --quiet -r sdd_generator\requirements.txt || goto :err
.venv\Scripts\python -m sdd_generator.web %*
if errorlevel 1 pause
goto :eof
:err
echo Setup failed. Install Python 3.10 or later from python.org and try again.
pause
