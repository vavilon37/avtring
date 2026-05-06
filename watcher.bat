@echo off
setlocal enabledelayedexpansion

set REPO_DIR=%~dp0
set PYTHON=python
set MAIN_SCRIPT=main.py
set CHECK_INTERVAL=10
set BOT_PID=

echo [WATCHER] Starting auto-deploy watcher...
echo [WATCHER] Repo: %REPO_DIR%
echo [WATCHER] Checking for updates every %CHECK_INTERVAL% seconds

cd /d "%REPO_DIR%"

:: Initial pull and start
git pull origin main
echo [WATCHER] Starting bot...
start /B "" %PYTHON% %MAIN_SCRIPT% > bot_output.log 2>&1
for /f %%i in ('powershell -command "(Get-Process python | Sort-Object StartTime -Descending | Select-Object -First 1).Id"') do set BOT_PID=%%i
echo [WATCHER] Bot started (PID: %BOT_PID%)

:: Store current commit hash
for /f %%i in ('git rev-parse HEAD') do set LAST_COMMIT=%%i

:loop
timeout /t %CHECK_INTERVAL% /nobreak >nul

:: Fetch remote changes
git fetch origin main >nul 2>&1

:: Get remote commit hash
for /f %%i in ('git rev-parse origin/main') do set REMOTE_COMMIT=%%i

if not "!LAST_COMMIT!"=="!REMOTE_COMMIT!" (
    echo [WATCHER] New commit detected: !REMOTE_COMMIT:~0,8!
    echo [WATCHER] Pulling changes...
    git pull origin main

    echo [WATCHER] Restarting bot...
    taskkill /F /IM python.exe >nul 2>&1
    timeout /t 2 /nobreak >nul
    start /B "" %PYTHON% %MAIN_SCRIPT% > bot_output.log 2>&1

    set LAST_COMMIT=!REMOTE_COMMIT!
    echo [WATCHER] Bot restarted successfully
)

goto loop
