@echo off
rem ChessCoach launcher for Windows. Double-click to start (the first run installs
rem everything for you), then it opens the app in your browser. Close this window to stop.
setlocal
cd /d "%~dp0"
title ChessCoach

if not exist ".venv\Scripts\python.exe" (
  echo First-time setup - installing Python dependencies and Stockfish...
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
)

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Setup did not complete - see the messages above.
  pause
  exit /b 1
)

echo.
echo   ChessCoach is starting at  http://127.0.0.1:6464
echo   Close this window to stop the server.
echo.
start "" http://127.0.0.1:6464
".venv\Scripts\python.exe" -m coach.web
