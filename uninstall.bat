@echo off
setlocal
title Dictation Helper - Uninstall
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
cd /d "%APPDIR%"

echo ============================================================
echo   Dictation ^& Read-Aloud Helper - Uninstall
echo ============================================================
echo.
echo This will:
echo   - stop the helper if it is running
echo   - delete the downloaded speech models (the .cache folder)
echo   - delete your settings, custom words and log files
echo   - remove the "start at login" shortcut, if you made one
echo   - delete the app files in this folder:
echo       %APPDIR%
echo.
echo Python itself is NOT removed.
echo.
choice /c YN /m "Continue with the uninstall"
if errorlevel 2 goto :cancel

echo.
echo [1/5] Stopping the helper...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -notmatch 'powershell' -and $_.CommandLine -like '*dictation_app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul

echo [2/5] Deleting speech models, settings and logs...
if exist ".cache" rd /s /q ".cache"
if exist "__pycache__" rd /s /q "__pycache__"
del /q "settings.json" "custom_words.txt" "dictation_app.log*" 2>nul
if exist ".cache" echo       WARNING: could not delete .cache - quit the helper from the tray and run this again.

echo [3/5] Removing the start-at-login shortcut...
powershell -NoProfile -Command "$sh = New-Object -ComObject WScript.Shell; Get-ChildItem ([Environment]::GetFolderPath('Startup')) -Filter *.lnk | Where-Object { "$($sh.CreateShortcut($_.FullName).TargetPath)".StartsWith($env:APPDIR + '\', 'OrdinalIgnoreCase') } | Remove-Item -Force"

echo [4/5] Python packages
echo       The helper installed about 600 MB of Python packages - faster-whisper, kokoro-onnx, etc.
echo       Keep them if you will reinstall soon: the next first run is then much quicker.
echo       Common packages that other programs may use - numpy, pillow - are always kept.
choice /c YN /m "      Remove the helper's Python packages too"
if errorlevel 2 goto :skip_packages
python -m pip uninstall -y faster-whisper ctranslate2 av tokenizers onnxruntime kokoro-onnx espeakng-loader phonemizer phonemizer-fork sounddevice pynput pyperclip pystray truststore
python -m pip cache purge
:skip_packages

echo [5/5] Deleting the app files...
del /q "dictation_app.py" "README.md" "requirements.txt" ".gitignore" "run_first_time.bat" "run_silent.bat" 2>nul

echo.
echo Uninstall complete. If a helper icon is still in the system tray,
echo it will disappear when you move the mouse over it.
echo.
echo Press any key to close - this uninstaller then deletes itself.
pause >nul
REM Deletes this file, then the folder if it is now empty (never anything else).
cd /d "%TEMP%" & (goto) 2>nul & del "%~f0" & rd "%APPDIR%" 2>nul

:cancel
echo.
echo Nothing was changed.
pause
exit /b
