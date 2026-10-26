<#
.SYNOPSIS
    多模态沉浸式智能助教 · 一键启动（Windows 主逻辑）

.DESCRIPTION
    由根目录的「1. 启动网页端(Windows).bat」调用。那个 .bat 只有纯 ASCII 几行，
    中文全部集中在本文件里 —— 因为 .bat 的编码行为随系统代码页变化，
    而 PowerShell 读 UTF-8 with BOM 的 .ps1 在任何语言版本的 Windows 上都一致。

.PARAMETER NoPause
    结束时不等待回车（自动化测试用）。

.PARAMETER NoBrowser
    不自动打开浏览器（自动化测试用）。

.NOTES
    本文件必须以 UTF-8 with BOM 保存。存成「UTF-8 无 BOM」时 Windows PowerShell 5.1
    会按 ANSI 代码页解读，中文提示全变乱码；存成 GBK 则英文 Windows 上乱码。
#>
param(
    [switch]$NoPause,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

# 控制台切到 UTF-8：让中文提示在非中文 Windows 上也能正常显示。
# 只在当前窗口生效，不影响后面启动的前后端窗口。
try {
    chcp 65001 > $null
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 极端情况下（旧版控制台）忽略即可，不影响启动
}

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$VenvPython = Join-Path $BackendDir '.venv\Scripts\python.exe'

function Write-Step([string]$Text) { Write-Host " $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host " $Text" -ForegroundColor Green }

function Fail([string]$Text, [string[]]$Hints) {
    Write-Host ''
    Write-Host " [X] $Text" -ForegroundColor Red
    foreach ($Hint in $Hints) { Write-Host "     $Hint" -ForegroundColor DarkGray }
    Write-Host ''
    if (-not $NoPause) { Read-Host ' 按回车键关闭此窗口' | Out-Null }
    exit 1
}

Write-Host ''
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host '   多模态沉浸式智能助教  ·  一键启动' -ForegroundColor White
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host ''

# ---------- 0. 环境检查 ----------
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail '未找到 Python。' @(
        '请安装 Python 3.12+，安装时务必勾选 "Add python.exe to PATH"',
        '下载：https://www.python.org/downloads/'
    )
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Fail '未找到 Node.js。' @(
        '请安装 Node.js 20 或更高版本',
        '下载：https://nodejs.org/'
    )
}

# ---------- 1. 后端运行环境 ----------
if (-not (Test-Path $VenvPython)) {
    Write-Step '[1/5] 首次运行：创建 Python 虚拟环境...'
    python -m venv (Join-Path $BackendDir '.venv')
    if ($LASTEXITCODE -ne 0) {
        Fail '创建虚拟环境失败。' @(
            '请确认 Python 版本 >= 3.12：python --version',
            '如提示缺少 venv 模块，请重新安装 Python 并勾选全部组件'
        )
    }

    Write-Step '[1/5] 安装后端依赖（约 1-2 分钟，请耐心等待）...'
    & $VenvPython -m pip install -q --upgrade pip
    & $VenvPython -m pip install -q -r (Join-Path $BackendDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        Fail '后端依赖安装失败。常见原因：' @(
            "网络问题：& '$VenvPython' -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend\requirements.txt",
            '缺少编译工具：安装 Visual Studio Build Tools',
            '公司代理：先设置 HTTP_PROXY / HTTPS_PROXY 环境变量'
        )
    }
    Write-Ok '[1/5] 后端依赖安装完成'
} else {
    Write-Ok '[1/5] 后端虚拟环境已就绪'
}

# ---------- 2. 配置文件 ----------
$EnvFile = Join-Path $BackendDir '.env'
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $BackendDir '.env.example') $EnvFile -ErrorAction SilentlyContinue
    Write-Step '[2/5] 已创建 backend\.env（当前为演示模式，未配置真实模型）'
    Write-Host '       要接入真实模型：编辑该文件并填写 LLM_API_KEY' -ForegroundColor DarkGray
} else {
    Write-Ok '[2/5] backend\.env 已存在'
}

# ---------- 3. 前端依赖 ----------
if (-not (Test-Path (Join-Path $FrontendDir 'node_modules'))) {
    Write-Step '[3/5] 首次运行：安装前端依赖（约 1-3 分钟）...'
    Push-Location $FrontendDir
    npm install
    $npmExit = $LASTEXITCODE
    Pop-Location
    if ($npmExit -ne 0) {
        Fail 'npm install 失败。' @(
            '网络问题：npm config set registry https://registry.npmmirror.com',
            '然后重新运行本脚本'
        )
    }
    Write-Ok '[3/5] 前端依赖安装完成'
} else {
    Write-Ok '[3/5] 前端依赖已就绪'
}

# ---------- 4. 示例教材 ----------
if (-not (Test-Path (Join-Path $BackendDir 'data\sample_textbook.pdf'))) {
    Write-Step '[4/5] 生成示例教材（113 页）...'
    Push-Location $BackendDir
    & $VenvPython 'scripts\make_sample_pdf.py' | Out-Null
    Pop-Location
    Write-Ok '[4/5] 示例教材已生成'
} else {
    Write-Ok '[4/5] 示例教材已存在'
}

# ---------- 5. 启动前后端 ----------
Write-Step '[5/5] 启动服务...'
Write-Host ''

# 两个独立窗口，标题即用途。用 Start-Sleep 而不是 timeout.exe：
# timeout.exe 在「输入被重定向」的环境（如 Git Bash 里调 cmd）会直接报错退出。
$BackendCmd = "`$Host.UI.RawUI.WindowTitle='智能助教 · 后端 :8000'; Set-Location '$BackendDir'; & '.\.venv\Scripts\python.exe' -m uvicorn app.main:app --reload --port 8000"
$FrontendCmd = "`$Host.UI.RawUI.WindowTitle='智能助教 · 前端 :5173'; Set-Location '$FrontendDir'; npm run dev"

Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoExit', '-Command', $BackendCmd) -WorkingDirectory $BackendDir | Out-Null
Start-Sleep -Seconds 3
Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoExit', '-Command', $FrontendCmd) -WorkingDirectory $FrontendDir | Out-Null

Write-Host ' 等待服务就绪（约 10 秒）...' -ForegroundColor DarkGray
Start-Sleep -Seconds 10

if (-not $NoBrowser) {
    Start-Process 'http://localhost:5173' | Out-Null
}

Write-Host ''
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host '   启动完成' -ForegroundColor White
Write-Host ''
Write-Host '   前端界面   http://localhost:5173' -ForegroundColor Gray
Write-Host '   接口文档   http://127.0.0.1:8000/docs' -ForegroundColor Gray
Write-Host '   健康检查   http://127.0.0.1:8000/api/health' -ForegroundColor Gray
Write-Host ''
Write-Host '   停止服务：关闭弹出的两个服务窗口' -ForegroundColor Gray
Write-Host '   安装为桌面应用：在 Chrome/Edge 地址栏右侧点「安装」图标' -ForegroundColor Gray
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host ''

if (-not $NoPause) {
    Read-Host ' 按回车键关闭此窗口' | Out-Null
}
