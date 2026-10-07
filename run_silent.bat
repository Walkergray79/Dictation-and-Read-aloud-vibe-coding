@echo off
REM Normal use: starts the helper in the system tray with no console window.
REM Put a shortcut to this file in shell:startup to launch it at login.
cd /d "%~dp0"
start "" pythonw dictation_app.py
