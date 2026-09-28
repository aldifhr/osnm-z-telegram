@echo off
REM Double-clickable wrapper around run-bot.ps1.
REM Scheduled tasks and services call run-bot.ps1 directly; this exists so a
REM human can start the bot by double-clicking and still see the output.
setlocal
cd /d "%~dp0"
where powershell >nul 2>&1 || (echo [!] powershell.exe not found & exit /b 1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-bot.ps1" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
    echo.
    echo [x] exited with code %RC%
    pause
)
endlocal & exit /b %RC%
