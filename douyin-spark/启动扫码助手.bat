@echo off
chcp 65001 >nul
title 续火花 - 本机扫码助手（保持运行）
rem 切到脚本所在目录（%~dp0 自带结尾反斜杠），换机器也能用
cd /d "%~dp0"

echo ==================================================
echo   本机扫码助手
echo   启动后保持运行（可最小化），网页点登录会自动弹窗
echo   关闭本窗口 = 停止接收扫码请求
echo ==================================================
echo.

set KEY=
if exist "%~dp0local-key.txt" set /p KEY=<"%~dp0local-key.txt"
if "%KEY%"=="" set /p KEY=请输入控制台口令: 
set SRV=
if exist "%~dp0server.txt" set /p SRV=<"%~dp0server.txt"
if "%SRV%"=="" set /p SRV=请输入服务器地址(如 http://1.2.3.4): 
".venv\Scripts\python.exe" spark-agent.py %SRV% %KEY%

echo.
echo 助手已退出
pause >nul
