@echo off
REM First run / troubleshooting: shows a console window with progress and errors.
cd /d "%~dp0"
call "%~dp0find_python.bat"
if not defined PY goto :nopython
echo Using Python: %PY%
echo.
"%PY%" %PYFLAGS% dictation_app.py
pause
exit /b

:nopython
echo Python was not found.
echo Install Python 3.12 (python.org, "Install Now", tick "Add python.exe to PATH"),
echo or use the portable bundle - see README.md.
pause
