# -*- coding: utf-8 -*-
"""一次性脚本 v3：bat 改为 CRLF 重写 + 报告 5280/5273 占用进程。用完即删。"""
import os
import socket
from pathlib import Path

# 一次性脚本：项目根按自身位置推导（可用环境变量 RAG_PROJ 覆盖），避免硬编码本机路径
base = Path(os.environ.get("RAG_PROJ") or Path(__file__).resolve().parent.parent)

start_bat = "\r\n".join([
    "@echo off",
    "rem RAG portable launcher - double-click friendly (2026-09-21)",
    "rem RAG_NO_WINDOW=1 skips the standalone app window (used by automated tests)",
    'set "RAG_NO_BROWSER=1"',
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0启动-RAG助手.ps1"',
    "if errorlevel 1 (",
    "  echo.",
    "  echo [START] failed. See messages above. Press any key to close.",
    "  pause >nul",
    "  exit /b %errorlevel%",
    ")",
    "if defined RAG_NO_WINDOW exit /b 0",
    'set "EDGE1=%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe"',
    'set "EDGE2=%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe"',
    "if exist %EDGE1% (",
    '  start "" %EDGE1% --app=http://127.0.0.1:5273/',
    ") else if exist %EDGE2% (",
    '  start "" %EDGE2% --app=http://127.0.0.1:5273/',
    ") else (",
    '  start "" http://127.0.0.1:5273/',
    ")",
    "",
])

stop_bat = "\r\n".join([
    "@echo off",
    "rem RAG portable stopper - double-click friendly (2026-09-21)",
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0停止-RAG助手.ps1"',
    "echo.",
    "echo Press any key to close this window.",
    "pause >nul",
    "",
])

(base / "启动-RAG助手.bat").write_bytes(start_bat.encode("gbk"))
(base / "停止-RAG助手.bat").write_bytes(stop_bat.encode("gbk"))
print("bat rewritten with CRLF")

for port in (5280, 5273):
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port))
        print(f"port {port}: LISTENING")
    except OSError:
        print(f"port {port}: free")
    finally:
        s.close()
