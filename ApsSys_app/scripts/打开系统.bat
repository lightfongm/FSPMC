@echo off
title PeacockScheduler V3.0

echo ========================================
echo   PeacockScheduler V3.0 Starting...
echo ========================================

REM Ensure MySQL is running
echo [1/3] Checking MySQL...
tasklist | findstr mysqld >nul 2>&1
if errorlevel 1 (
    echo Starting MySQL...
    start "" "C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe" --no-defaults --user=root --datadir="D:\AI\workbuddy\PMC\mysql_data" --basedir="C:\Program Files\MySQL\MySQL Server 8.0" --port=3306
    timeout /t 5 /nobreak >nul
) else (
    echo MySQL is already running.
)

REM Start Flask backend
echo [2/3] Starting Flask backend (port 5000)...
cd /d "D:\AI\workbuddy\PMC\ApsSys_app"
set APS_DEBUG=0
set APS_HOST=0.0.0.0
start "" "C:\Users\Fon\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe" -m backend.run

REM Wait for service ready
echo [3/3] Waiting for service...
timeout /t 4 /nobreak >nul

REM Open browser
echo Opening browser...
start "" "http://localhost:5000"

echo ========================================
echo   System started! URL: http://localhost:5000
echo   LAN access: http://DESKTOP-EBV5GRB:5000
echo   Press any key to close this window.
echo ========================================
pause >nul
