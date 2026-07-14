@echo off
chcp 65001 >nul
title 黄金价格看板
cd /d "%~dp0"
echo ========================================
echo   黄金价格监控 HTTP 服务
echo   启动后访问 http://localhost:8765
echo ========================================
echo.
python gold_server.py
pause
