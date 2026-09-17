@echo off
cd /d "%~dp0"
title TWSE Dashboard  --  close this window to stop the server

rem Do not start a second instance: it would collide on port 5000 and die
rem instantly, leaving the user with a black window that flashed and vanished.
python -c "import socket,sys;s=socket.socket();r=s.connect_ex(('127.0.0.1',5000));s.close();sys.exit(0 if r==0 else 1)" 2>nul
if %errorlevel%==0 goto already

echo.
echo   Starting TWSE Dashboard...
echo   First load takes 20-40 seconds. The browser opens automatically.
echo.
echo   [ To stop the server, just close this window ]
echo.

rem Wait until the server actually answers before opening the browser.
rem A fixed sleep would open a dead page on a slower machine.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 90;$i++){try{$r=Invoke-WebRequest -Uri 'http://127.0.0.1:5000/healthz' -TimeoutSec 2 -UseBasicParsing;if($r.StatusCode -eq 200){Start-Process 'http://127.0.0.1:5000';break}}catch{};Start-Sleep -Seconds 2}"

python server.py
echo.
echo   Server stopped.
pause
exit /b

:already
echo.
echo   Server is already running. Opening the browser.
echo.
start "" http://127.0.0.1:5000
timeout /t 3 >nul
exit /b
