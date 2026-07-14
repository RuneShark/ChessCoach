@echo off
rem One-time setup so the ChessCoach UI answers at  http://chesscoach  (no port),
rem reachable ONLY from this PC. It does two Windows-side things:
rem   1. hosts entry   127.0.0.1  chesscoach      (a private name for loopback)
rem   2. port-proxy    127.0.0.1:80 -> 127.0.0.1:<port>   (so the URL needs no :port)
rem Everything stays on 127.0.0.1 (loopback) - nothing is exposed to your network.
rem Re-run any time to repair (e.g. after changing the port). Needs admin (UAC).
setlocal EnableExtensions
set "NAME=chesscoach"
set "PORT=6464"
rem keep in sync with serve.conf (the single source of truth for the port)
for /f "usebackq tokens=2 delims==" %%v in (`findstr /b /i "COACH_PORT" "%~dp0serve.conf" 2^>nul`) do set "PORT=%%v"
set "PORT=%PORT: =%"
set "HOSTS=%SystemRoot%\System32\drivers\etc\hosts"

rem --- self-elevate if not already admin ---
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator rights...
  powershell -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
  exit /b
)

echo.
echo === ChessCoach URL setup  (name=%NAME%  port=%PORT%) ===
echo.

rem --- 1) hosts entry (add only if the name isn't there yet) ---
findstr /i /c:" %NAME%" "%HOSTS%" >nul 2>&1
if %errorlevel% neq 0 (
  (echo 127.0.0.1  %NAME%)>>"%HOSTS%"
  echo   [+] added hosts entry: 127.0.0.1  %NAME%
) else (
  echo   [=] hosts entry already present
)

rem --- 2) loopback port proxy 80 -> PORT (refresh it in case the port changed) ---
netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=80 >nul 2>&1
netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=80 connectaddress=127.0.0.1 connectport=%PORT% >nul 2>&1
echo   [+] port proxy 127.0.0.1:80 -^> 127.0.0.1:%PORT%
echo.
netsh interface portproxy show v4tov4

echo.
echo Done. Start the app (ChessCoach.bat), then open:  http://%NAME%
echo.
echo To undo later, run as admin:
echo   netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=80
echo   ^(and remove the "127.0.0.1  %NAME%" line from %HOSTS%^)
echo.
pause
