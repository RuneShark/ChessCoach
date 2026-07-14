@echo off
rem ChessCoach uninstaller for Windows. Double-click to remove the virtualenv, the
rem downloaded engine, and your games / config / generated reports from this folder.
rem It does NOT remove Python. Your profile.md is kept.
setlocal
cd /d "%~dp0"
title ChessCoach uninstaller

echo This removes ChessCoach's virtualenv, downloaded engine, and your games / config /
echo generated reports from this folder. Python is NOT removed; profile.md is kept.
echo.
set "OK="
set /p "OK=Continue? [y/N] "
if /i not "%OK%"=="y" ( echo Cancelled. & pause & exit /b 0 )

echo Stopping the server (if running)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*coach.web*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

echo Removing files...
if exist ".venv"        rmdir /s /q ".venv"
if exist "stockfish.exe" del /q "stockfish.exe"
if exist "config.json"  del /q "config.json"
if exist ".web.log"     del /q ".web.log"
if exist ".web.pid"     del /q ".web.pid"
del /q "data\games\*.pgn" "data\games\*.jsonl" "data\analysis\*.json" "data\*.jsonl" "data\*.json" >nul 2>&1
for %%f in (report tilt accuracy endgame_conversion progress drill_progress weaknesses plan) do (
  if exist "journal\%%f.md" del /q "journal\%%f.md"
)
for /d /r %%d in (__pycache__) do if exist "%%d" rmdir /s /q "%%d"

echo.
echo Done. To finish, delete this folder:
echo    %~dp0
echo.
echo If you set up the clean http://chesscoach URL (setup-url.bat), remove it as admin:
echo    netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=80
echo    (and delete the "127.0.0.1  chesscoach" line from
echo     %SystemRoot%\System32\drivers\etc\hosts)
echo Python, if the installer added it, can be removed via Settings ^> Apps.
echo.
pause
