@echo off
rem RAG portable launcher - double-click friendly (2026-09-21)
rem Opens the assistant page as a standalone app window (Edge app mode).
rem RAG_NO_WINDOW=1 skips the app window (used by automated tests).
set "RAG_NO_BROWSER=1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-rag.ps1"
if errorlevel 1 (
  echo.
  echo [START] failed. See messages above. Press any key to close.
  pause >nul
  exit /b %errorlevel%
)
if defined RAG_NO_WINDOW exit /b 0
set "EDGE1=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set "EDGE2=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if exist %EDGE1% (
  start "" %EDGE1% --app=http://127.0.0.1:5273/
) else if exist %EDGE2% (
  start "" %EDGE2% --app=http://127.0.0.1:5273/
) else (
  start "" http://127.0.0.1:5273/
)
