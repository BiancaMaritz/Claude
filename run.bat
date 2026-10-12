@echo off
rem Launch the SDD Generator web UI at http://localhost:8765/
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install --quiet python-docx || goto :err
)
.venv\Scripts\python -m sdd_generator.web %*
if errorlevel 1 pause
goto :eof
:err
echo Setup failed. Install Python 3.10 or later from python.org and try again.
pause
