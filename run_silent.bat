@echo off
REM Normal use: starts the helper in the system tray with no console window.
REM Put a shortcut to this file in shell:startup to launch it at login.
cd /d "%~dp0"
call "%~dp0find_python.bat"
if not defined PY (
    echo Python was not found - run run_first_time.bat for details.
    pause
    exit /b 1
)
start "" "%PYW%" %PYFLAGS% dictation_app.py
