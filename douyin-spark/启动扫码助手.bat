@echo off
chcp 65001 >nul
title 续火花 - 本机扫码助手（保持运行）
rem 切到脚本所在目录（%~dp0 自带结尾反斜杠），换机器也能用
cd /d "%~dp0"

echo ==================================================
echo   本机扫码助手 v1.1
echo   启动后保持运行（可最小化），网页点登录会自动弹窗
echo   关闭本窗口 = 停止接收扫码请求
echo ==================================================
echo.

rem ---------------- 读配置：server.txt / local-key.txt ----------------
rem 用 for /f 而不是 set /p：set /p 会把 BOM 和行尾空格一起读进来，
rem 结果就是"口令看着对、服务器却一直 401"这种最难查的问题。
set "SRV="
if exist "%~dp0server.txt" (
  for /f "usebackq delims=" %%a in ("%~dp0server.txt") do if not defined SRV set "SRV=%%a"
)
set "KEY="
if exist "%~dp0local-key.txt" (
  for /f "usebackq delims=" %%a in ("%~dp0local-key.txt") do if not defined KEY set "KEY=%%a"
)

if "%SRV%"=="" (
  echo 没找到 server.txt，请输入服务器地址，例如 http://154.8.168.48
  set /p "SRV=服务器地址: "
)
if "%KEY%"=="" (
  echo 没找到 local-key.txt，请输入控制台【管理员口令】
  set /p "KEY=管理员口令: "
)

rem ---------------- 找 Python ----------------
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo.
    echo [错误] 找不到 Python，也找不到 .venv\Scripts\python.exe
    echo        请先在项目目录执行：python -m venv .venv
    echo        然后执行：.venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
  set "PY=python"
  echo [提示] 没有 .venv，改用系统 Python
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo.
echo 正在连接 %SRV% ...
echo.
"%PY%" spark-agent.py "%SRV%" "%KEY%"

echo.
echo ------------------------------------------------
echo 助手已退出（错误码 %errorlevel%）。
echo 若上面写着「口令不对」，改 local-key.txt 里那一行再重开；
echo 若写着「连不上服务器」，先确认本机能打开 %SRV%。
echo 详细日志：agent.log
echo ------------------------------------------------
pause >nul
