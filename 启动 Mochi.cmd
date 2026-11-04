@echo off
chcp 65001 >nul
title Mochi
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   没找到 python。请先安装 Python 3.11+ 并勾选 "Add to PATH"。
  echo.
  pause
  exit /b 1
)

echo.
echo   Mochi 启动中…浏览器会自动打开
echo   关闭浏览器后服务会自动停止；这个窗口可以留着看日志。
echo   想立刻停：按 Ctrl+C 或直接关掉这个窗口。
echo.

python web\server.py --port 8765 --data data --open

if errorlevel 1 (
  echo.
  echo   启动失败。常见原因：
  echo     - 端口 8765 被别的程序占用   → 换端口：python web\server.py --port 8766 --open
  echo     - 缺少依赖                    → python -m pip install sqlcipher3 httpx
  echo.
  pause
)
