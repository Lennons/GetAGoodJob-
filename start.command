#!/bin/bash
cd "$(dirname "$0")"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  BOSS 直聊助手 v1.1.0"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Check Python ──
if ! command -v python3 &>/dev/null; then
    echo "❌ 未找到 python3，请先安装 Python 3.11+"
    echo "   下载：https://www.python.org/downloads/"
    read -p "按任意键退出..."
    exit 1
fi

PYVER=$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Python $PYVER"

# ── Create venv if needed ──
if [ ! -d ".venv" ]; then
    echo "⏳ 创建虚拟环境..."
    python3 -m venv .venv
fi

# ── Activate venv ──
source .venv/bin/activate

# ── Install deps ──
echo "⏳ 检查依赖..."
pip install -q -r requirements.txt 2>/dev/null

# ── Check MySQL ──
echo "⏳ 检查 MySQL..."
python3 -c "
import pymysql
try:
    c=pymysql.connect(host='127.0.0.1',port=3306,user='boss_assistant',password='b20ab056fc747b35b6f56129a642c0214d48bee42fad59f0',database='boss_chat_assistant',charset='utf8mb4')
    c.close()
    print('✓ MySQL 已连接')
except Exception as e:
    print(f'⚠ MySQL 连接失败: {e}')
    print('  请确保 MySQL 正在运行')
"

# ── Start server ──
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  正在启动服务..."
echo "  浏览器打开 http://127.0.0.1:8788"
echo "  按 Ctrl+C 停止"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

sleep 1
open http://127.0.0.1:8788 2>/dev/null || true
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8788 --log-level info
