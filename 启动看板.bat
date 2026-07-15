@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
title Gold Dashboard
cd /d "%~dp0"
echo ========================================
echo   Gold Price Monitor / HTTP Service
echo   http://localhost:8765
echo ========================================
echo.
python gold_server.py
pause
