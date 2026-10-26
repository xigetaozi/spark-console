@echo off
chcp 65001 >nul
title 抖音扫码登录取证（仅扫码，不启动服务）
cd /d "%~dp0"

echo ============================================
echo   仅扫码模式：不会启动 Web 服务、不会发消息
echo ============================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scan-only.ps1"

echo.
echo 窗口将在按键后关闭
pause >nul
