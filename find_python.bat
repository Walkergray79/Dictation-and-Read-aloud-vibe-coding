@echo off
REM Used by the other .bat files. Sets PY (console Python), PYW (no console)
REM and PYFLAGS. Looks for, in order:
REM   1. a portable Python in a "python" folder next to the app (portable bundle)
REM   2. a WinPython folder (WPy64-...) next to the app
REM   3. the Python installed on this PC
set "PY="
set "PYW="
set "PYFLAGS="
if exist "%~dp0python\python.exe" (
    set "PY=%~dp0python\python.exe"
    set "PYW=%~dp0python\pythonw.exe"
    set "PYFLAGS=-s"
    goto :eof
)
for /d %%W in ("%~dp0WPy64*") do (
    for /d %%P in ("%%W\python-3*" "%%W\python") do (
        if exist "%%P\python.exe" (
            set "PY=%%P\python.exe"
            set "PYW=%%P\pythonw.exe"
            set "PYFLAGS=-s"
        )
    )
)
if defined PY goto :eof
where python >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    set "PYW=pythonw"
)
