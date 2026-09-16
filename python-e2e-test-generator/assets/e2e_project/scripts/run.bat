@echo off
setlocal
rem Usage: scripts\run.bat                         (run all scenarios)
rem Usage: scripts\run.bat --scenario "scenario"  (run one scenario)
set "SCRIPT_DIR=%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"
python "%SCRIPT_DIR%run_e2e.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
