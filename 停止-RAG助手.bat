@echo off
rem RAG portable stopper - double-click friendly (2026-09-21)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-rag.ps1"
echo.
echo Press any key to close this window.
pause >nul
