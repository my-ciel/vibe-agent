@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

where uv >nul 2>nul
if %errorlevel%==0 goto run

set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
where uv >nul 2>nul
if %errorlevel%==0 goto run

echo.
echo [Vibe Agent] The helper tool "uv" is not installed yet.
echo uv sets up Python and the required packages automatically.
echo It is installed once and takes about 1 minute.
echo Source: https://astral.sh/uv  (official installer, open source)
echo.
set /p ANSWER=Install uv now? Type Y and press Enter: 
if /i not "%ANSWER%"=="Y" goto cancelled

powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
where uv >nul 2>nul
if not %errorlevel%==0 goto failed

:run
uv run python launch.py %*
set EXITCODE=%errorlevel%
if not %EXITCODE%==0 pause
exit /b %EXITCODE%

:cancelled
echo.
echo Cancelled. Ask your instructor for the manual install steps.
pause
exit /b 1

:failed
echo.
echo uv could not be installed automatically.
echo Your network or security settings may block it. Show this window to your instructor.
pause
exit /b 1
