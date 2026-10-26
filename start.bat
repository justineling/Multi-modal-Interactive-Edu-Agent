@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ============================================================
echo    多模态沉浸式智能助教  ·  一键启动
echo  ============================================================
echo.

REM ---------- 0. 环境检查 ----------
where python >nul 2>nul
if errorlevel 1 (
    echo  [X] 未找到 Python。
    echo      请安装 Python 3.12+，安装时务必勾选 "Add python.exe to PATH"
    echo      下载：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
    echo  [X] 未找到 Node.js。
    echo      请安装 Node.js 20 或更高版本
    echo      下载：https://nodejs.org/
    echo.
    pause
    exit /b 1
)

REM ---------- 1. 后端虚拟环境 ----------
if not exist "backend\.venv\Scripts\python.exe" (
    echo  [1/5] 首次运行：创建 Python 虚拟环境...
    python -m venv backend\.venv
    if errorlevel 1 (
        echo  [X] 创建虚拟环境失败
        pause
        exit /b 1
    )

    echo  [1/5] 安装后端依赖（约 1-2 分钟，请耐心等待）...
    backend\.venv\Scripts\python.exe -m pip install -q --upgrade pip
    backend\.venv\Scripts\python.exe -m pip install -q -r backend\requirements.txt
    if errorlevel 1 (
        echo.
        echo  [X] 后端依赖安装失败。常见原因：
        echo      - 网络问题：试试 pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend\requirements.txt
        echo      - 缺少编译工具：安装 Visual Studio Build Tools
        echo.
        pause
        exit /b 1
    )
    echo  [1/5] 后端依赖安装完成
) else (
    echo  [1/5] 后端虚拟环境已就绪
)

REM ---------- 2. 环境变量 ----------
if not exist "backend\.env" (
    copy /y "backend\.env.example" "backend\.env" >nul 2>nul
    echo  [2/5] 已创建 backend\.env（当前为演示模式，未接真实模型）
    echo        要接真实模型：编辑该文件，填入 LLM_API_KEY
) else (
    echo  [2/5] backend\.env 已存在
)

REM ---------- 3. 前端依赖 ----------
if not exist "frontend\node_modules" (
    echo  [3/5] 首次运行：安装前端依赖（约 1-3 分钟）...
    pushd frontend
    call npm install
    if errorlevel 1 (
        echo  [X] npm install 失败，检查网络或换用国内镜像：
        echo      npm config set registry https://registry.npmmirror.com
        popd
        pause
        exit /b 1
    )
    popd
    echo  [3/5] 前端依赖安装完成
) else (
    echo  [3/5] 前端依赖已就绪
)

REM ---------- 4. 样本教材 ----------
if not exist "backend\data\sample_textbook.pdf" (
    echo  [4/5] 生成样本教材（113 页，用于演示）...
    pushd backend
    .venv\Scripts\python.exe scripts\make_sample_pdf.py >nul 2>nul
    popd
    echo  [4/5] 样本教材已生成
) else (
    echo  [4/5] 样本教材已存在
)

REM ---------- 5. 启动前后端 ----------
echo  [5/5] 启动服务...
echo.

cd /d "%~dp0backend"
start "智能助教 · 后端 :8000" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000"

ping -n 4 127.0.0.1 >nul

cd /d "%~dp0frontend"
start "智能助教 · 前端 :5173" cmd /k "npm run dev"

cd /d "%~dp0"

echo  等待服务就绪（约 10 秒）...
ping -n 11 127.0.0.1 >nul

start "" "http://localhost:5173"

echo.
echo  ============================================================
echo    启动完成
echo.
echo    前端界面   http://localhost:5173
echo    接口文档   http://127.0.0.1:8000/docs
echo    健康检查   http://127.0.0.1:8000/api/health
echo.
echo    停止服务：关闭弹出的两个命令行窗口
echo  ============================================================
echo.
pause
