@echo off
chcp 65001 >nul
title Spark Scan Helper
rem ASCII-only on purpose: any Chinese text in a .bat gets read byte-by-byte by cmd
rem and can be executed as a command ("'a' is not recognized..."). All Chinese
rem output comes from spark-agent.py instead.
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ==================================================
echo   Spark scan helper  (keep this window open)
echo   close window = stop receiving scan requests
echo ==================================================
echo.

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python not found: no .venv\Scripts\python.exe and no python in PATH
    echo         python -m venv .venv
    echo         .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
  set "PY=python"
  echo [info] using system python
)

rem server / key are read from server.txt and local-key.txt by spark-agent.py
"%PY%" "%~dp0spark-agent.py"

echo.
echo --------------------------------------------------
echo helper exited, code=%errorlevel%
echo   wrong password -^> edit local-key.txt, run again
echo   cannot connect -^> check server.txt, open it in a browser
echo   detail log     -^> agent.log
echo --------------------------------------------------
pause
