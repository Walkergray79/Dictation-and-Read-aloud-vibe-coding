@echo off
setlocal
title Dictation Helper - Make a portable bundle
cd /d "%~dp0"

echo ==================================================================
echo   Make a portable bundle of the Dictation ^& Read-Aloud Helper
echo ==================================================================
echo.
echo Run this ONCE on a PC with internet - for example at home.
echo It builds a folder with everything inside it:
echo   - its own portable Python (nothing is installed on Windows)
echo   - all the packages the helper needs
echo   - the speech models
echo Copy that folder (or the ZIP) to the work PC or a USB stick and run
echo run_first_time.bat or run_silent.bat inside it. No internet, no admin.
echo.
echo It downloads about 1 GB and takes 5-15 minutes.
echo.
set "OUT=%~dp0DictationHelper-portable"
set "ZIP=%~dp0DictationHelper-portable.zip"
set "WORK=%TEMP%\dh-bundle-%RANDOM%%RANDOM%"
set "EXTRA="
set "COPYWORDS=N"

choice /c YN /m "Continue"
if errorlevel 2 goto :eof
echo.
choice /c YN /m "Also include the Accurate dictation model (small.en, about 480 MB more)"
if not errorlevel 2 set "EXTRA=--whisper=small.en"
if exist "custom_words.txt" (
    choice /c YN /m "Include your custom words file"
    if not errorlevel 2 set "COPYWORDS=Y"
)
if exist "%OUT%" (
    echo.
    echo A bundle already exists in: %OUT%
    choice /c YN /m "Replace it"
    if errorlevel 2 goto :eof
    rd /s /q "%OUT%"
)
if exist "%ZIP%" del /q "%ZIP%"
mkdir "%WORK%" || goto :fail

echo.
echo [1/5] Downloading uv - a small tool that fetches a portable Python...
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '%WORK%\uv.zip'; Expand-Archive -Path '%WORK%\uv.zip' -DestinationPath '%WORK%\uv' -Force"
if not exist "%WORK%\uv\uv.exe" goto :fail

echo [2/5] Downloading a portable Python 3.12...
"%WORK%\uv\uv.exe" python install 3.12 --install-dir "%WORK%\py" --no-bin --no-registry
if errorlevel 1 goto :fail
mkdir "%OUT%"
for /d %%D in ("%WORK%\py\cpython-3.12.*") do (
    robocopy "%%D" "%OUT%\python" /E /NFL /NDL /NJH /NJS /NP >nul
)
if not exist "%OUT%\python\python.exe" goto :fail
REM uv marks its Pythons "externally managed"; this one is ours, so allow pip.
if exist "%OUT%\python\Lib\EXTERNALLY-MANAGED" del /q "%OUT%\python\Lib\EXTERNALLY-MANAGED"

echo [3/5] Copying the app files...
for %%F in (dictation_app.py README.md CHANGELOG.md requirements.txt find_python.bat run_first_time.bat run_silent.bat uninstall.bat) do (
    if exist "%%F" copy /y "%%F" "%OUT%\" >nul
)
if /i "%COPYWORDS%"=="Y" copy /y "custom_words.txt" "%OUT%\" >nul
REM Re-use models already downloaded on this PC instead of fetching them again.
if exist ".cache" robocopy ".cache" "%OUT%\.cache" /E /NFL /NDL /NJH /NJS /NP >nul

echo [4/5] Installing the packages and downloading the speech models...
"%OUT%\python\python.exe" -s "%OUT%\dictation_app.py" --download-models %EXTRA%
if errorlevel 1 goto :fail
del /q "%OUT%\dictation_app.log*" 2>nul
del /q "%OUT%\settings.json" 2>nul
if exist "%OUT%\__pycache__" rd /s /q "%OUT%\__pycache__"

echo [5/5] Making the ZIP (this can take a few minutes)...
tar -a -c -f "%ZIP%" -C "%~dp0." "DictationHelper-portable"
if errorlevel 1 echo       Could not make the ZIP - just copy the folder instead.

rd /s /q "%WORK%" 2>nul
echo.
echo ==================================================================
echo   Done!
echo   Folder: %OUT%
if exist "%ZIP%" echo   ZIP:    %ZIP%
echo.
echo   On the work PC: unzip or copy it to e.g. C:\Users\^<you^>\DictationHelper\
echo   then double-click run_first_time.bat inside it. No internet needed.
echo ==================================================================
pause
goto :eof

:fail
echo.
echo Something went wrong - see the messages above.
echo If a company proxy blocks downloads, run this on a home PC instead.
rd /s /q "%WORK%" 2>nul
pause
exit /b 1
