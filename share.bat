@echo off
cd /d "%~dp0"
title Public Share Tunnel  --  close this window to stop sharing

set CF="C:\Program Files (x86)\cloudflared\cloudflared.exe"
if not exist %CF% (
  echo.
  echo   cloudflared not found. Install it with:
  echo     winget install --id Cloudflare.cloudflared
  echo.
  pause
  exit /b
)

rem The dashboard must already be running: the tunnel only forwards to it.
python -c "import socket,sys;s=socket.socket();r=s.connect_ex(('127.0.0.1',5000));s.close();sys.exit(0 if r==0 else 1)" 2>nul
if not %errorlevel%==0 (
  echo.
  echo   The dashboard is not running. Start dashboard.bat first,
  echo   wait until the browser opens, then run this again.
  echo.
  pause
  exit /b
)

echo.
echo   Creating a public HTTPS link to your local dashboard...
echo.
echo   Look for a line like:
echo       https://something-random.trycloudflare.com
echo   That is the link to share. Anyone who has it can view the site.
echo.
echo   [ Close this window to stop sharing. The link dies with it. ]
echo.

%CF% tunnel --url http://127.0.0.1:5000 --no-autoupdate
echo.
echo   Sharing stopped.
pause
