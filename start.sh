#!/usr/bin/env bash
# 多模态沉浸式智能助教 · 一键启动（macOS / Linux）
# 用法：chmod +x start.sh && ./start.sh
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

echo ""
echo "============================================================"
echo "  多模态沉浸式智能助教 · 一键启动"
echo "============================================================"
echo ""

# ---------- 0. 环境检查 ----------
if ! command -v python3 >/dev/null 2>&1; then
    echo " [X] 未找到 python3。请安装 Python 3.12+"
    exit 1
fi
if ! command -v node >/dev/null 2>&1; then
    echo " [X] 未找到 node。请安装 Node.js 20+"
    exit 1
fi

# ---------- 1. 后端虚拟环境 ----------
VENV="$ROOT/backend/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo " [1/5] 首次运行：创建 Python 虚拟环境..."
    python3 -m venv "$VENV"
    echo " [1/5] 安装后端依赖（约 1-2 分钟）..."
    "$VENV/bin/python" -m pip install -q --upgrade pip
    "$VENV/bin/python" -m pip install -q -r backend/requirements.txt
    echo " [1/5] 后端依赖安装完成"
else
    echo " [1/5] 后端虚拟环境已就绪"
fi

# ---------- 2. 环境变量 ----------
if [ ! -f "$ROOT/backend/.env" ]; then
    cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
    echo " [2/5] 已创建 backend/.env（演示模式，未接真实模型）"
else
    echo " [2/5] backend/.env 已存在"
fi

# ---------- 3. 前端依赖 ----------
if [ ! -d "$ROOT/frontend/node_modules" ]; then
    echo " [3/5] 首次运行：安装前端依赖（约 1-3 分钟）..."
    (cd "$ROOT/frontend" && npm install)
    echo " [3/5] 前端依赖安装完成"
else
    echo " [3/5] 前端依赖已就绪"
fi

# ---------- 4. 样本教材 ----------
if [ ! -f "$ROOT/backend/data/sample_textbook.pdf" ]; then
    echo " [4/5] 生成样本教材..."
    (cd "$ROOT/backend" && "$VENV/bin/python" scripts/make_sample_pdf.py >/dev/null 2>&1)
    echo " [4/5] 样本教材已生成"
else
    echo " [4/5] 样本教材已存在"
fi

# ---------- 5. 启动 ----------
echo " [5/5] 启动服务..."
echo ""

BACK_PID=""
FRONT_PID=""

cleanup() {
    echo ""
    echo "正在停止服务..."
    [ -n "$FRONT_PID" ] && kill "$FRONT_PID" 2>/dev/null || true
    [ -n "$BACK_PID" ] && kill "$BACK_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    echo "已停止。"
}
trap cleanup EXIT INT TERM

(cd "$ROOT/backend" && exec "$VENV/bin/python" -m uvicorn app.main:app --reload --port 8000) &
BACK_PID=$!

sleep 3

(cd "$ROOT/frontend" && exec npm run dev) &
FRONT_PID=$!

echo " 等待服务就绪（约 10 秒）..."
sleep 10

URL="http://localhost:5173"
if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 || true
fi

echo ""
echo "============================================================"
echo "  启动完成"
echo ""
echo "  前端界面   $URL"
echo "  接口文档   http://127.0.0.1:8000/docs"
echo "  健康检查   http://127.0.0.1:8000/api/health"
echo ""
echo "  按 Ctrl+C 停止全部服务"
echo "============================================================"
echo ""

wait
