@echo off
chcp 65001 >nul
title BOSS 直聊助手 v1.1.1
cd /d "%~dp0"

echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo   BOSS 直聊助手 v1.1.1
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━

:: ── Check Python ──
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ❌ 未找到 Python，请先安装 Python 3.11+
    echo   下载：https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import sys;print(f'  Python {sys.version_info.major}.{sys.version_info.minor}')" 2>nul

:: ── Create venv if needed ──
if not exist ".venv\Scripts\python.exe" (
    echo ⏳ 创建虚拟环境...
    python -m venv .venv
)

:: ── Activate & install ──
call .venv\Scripts\activate.bat
echo ⏳ 检查依赖...
pip install -q -r requirements.txt 2>nul

:: ── Check MySQL ──
echo ⏳ 检查 MySQL...
python -c "import pymysql;c=pymysql.connect(host='127.0.0.1',port=3306,user='boss_assistant',password='b20ab056fc747b35b6f56129a642c0214d48bee42fad59f0',database='boss_chat_assistant',charset='utf8mb4');c.close();print('  MySQL OK')" 2>nul
if %errorlevel% neq 0 echo ⚠ MySQL 连接失败，请确保 MySQL 正在运行

:: ── Start ──
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo   正在启动服务...
echo   浏览器打开 http://127.0.0.1:8788
echo   按 Ctrl+C 停止
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━

start http://127.0.0.1:8788
python -m uvicorn app.main:app --host 0.0.0.0 --port 8788 --log-level info
pause
