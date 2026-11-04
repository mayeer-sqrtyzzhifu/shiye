@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 跑测试但不污染源码目录：
rem   -p no:cacheprovider  不生成 .pytest_cache
rem   PYTHONPYCACHEPREFIX  不生成 src/**/__pycache__

if "%LOCALAPPDATA%"=="" set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\shiye\pycache"

python -m pytest -p no:cacheprovider %*
