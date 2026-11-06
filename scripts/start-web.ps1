<#
.SYNOPSIS
    多模态沉浸式智能助教 · 一键启动（Windows 主逻辑）

.DESCRIPTION
    由根目录的「1. 启动网页端(Windows).bat」调用。那个 .bat 只有纯 ASCII 几行，
    中文全部集中在本文件里 —— 因为 .bat 的编码行为随系统代码页变化，
    而 PowerShell 读 UTF-8 with BOM 的 .ps1 在任何语言版本的 Windows 上都一致。

    只保留 1 个可见窗口（就是 .bat 打开的那个）：后端与前端都以
    Start-Process -WindowStyle Hidden 后台运行，输出重定向到日志文件。
    两个端口都就绪后才自动打开浏览器；按回车键杀整棵进程树并清理 PID 文件。

.PARAMETER NoPause
    不等待回车，且**不清理子进程** —— 服务有意留在后台供自动化使用。
    关掉窗口不会停止服务，需要手动跑「3. 停止网页端(Windows).bat」。

.PARAMETER NoBrowser
    不自动打开浏览器（自动化测试用）。

.PARAMETER TargetUrl
    两个端口都就绪后打开的浏览器地址。默认 http://localhost:5173。

.PARAMETER DryRun
    只做环境/依赖/端口检查，打印「将要执行的动作」后正常退出。
    不启动服务、不开浏览器、不等待、不写日志目录与 PID 文件（QA 无副作用验证用）。

.NOTES
    本文件必须以 UTF-8 with BOM 保存。存成「UTF-8 无 BOM」时 Windows PowerShell 5.1
    会按 ANSI 代码页解读，中文提示全变乱码；存成 GBK 则英文 Windows 上乱码。
#>
param(
    [switch]$NoPause,
    [switch]$NoBrowser,
    [string]$TargetUrl = 'http://localhost:5173',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# -NoPause 的语义是「不等待回车、不清理子进程」，服务有意留在后台。
# 退出事件里必须据此跳过清理，否则自动化模式刚起来的服务会被自己杀掉。
$script:KeepServicesOnExit = [bool]$NoPause

# 控制台切到 UTF-8：让中文提示在非中文 Windows 上也能正常显示。
try {
    chcp 65001 > $null
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 极端情况下（旧版控制台）忽略即可，不影响启动
}

# 本机设了 http_proxy 时，localhost 请求会被代理拦截返回 502。
# NO_PROXY 必须在这里设，子进程（uvicorn / vite）会继承这个环境变量。
# 就绪检测用裸 TCP 连接而不是 Invoke-WebRequest，也是为了绕开 .NET 默认代理。

function Normalize-ProxyEnvironment {
    <# 修掉「Start-Process 带重定向时抛 Item has already been added」的环境坑。

       本机宿主会同时注入 HTTP_PROXY 与 http_proxy（以及 HTTPS_PROXY/https_proxy），
       两种大小写都存在于进程环境块里，值相同。Windows PowerShell 5.1 的 Start-Process
       在带 -RedirectStandardOutput/-RedirectStandardError 时（即 UseShellExecute=false）
       会把环境块拷进一个**忽略大小写**的字典，遇到同名两种大小写就直接抛：
           Item has already been added. Key in dictionary: 'HTTPS_PROXY'
           Key being added: 'https_proxy'
       这不是脚本写错，是环境块本身有重复项 —— 连 Get-ChildItem Env: 都会同样报错。
       实测验证：在干净的 PowerShell 里单独跑一次带双重定向的 Start-Process 也会失败。

       处置：删掉小写写法、保留大写写法，值原样写回。Windows 环境变量查找大小写不敏感，
       所以外部模型请求需要的代理配置完全不变；子进程继承到的是已归一化的环境块。
       没有这一步，本脚本要求的「隐藏窗口 + 双日志重定向」在这台机器上根本跑不起来。
    #>
    foreach ($Name in @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')) {
        $Value = [Environment]::GetEnvironmentVariable($Name)
        [Environment]::SetEnvironmentVariable($Name.ToLowerInvariant(), $null)
        if ($null -ne $Value) {
            [Environment]::SetEnvironmentVariable($Name, $Value)
        }
    }
}

Normalize-ProxyEnvironment
$env:NO_PROXY = 'localhost,127.0.0.1'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$VenvPython = Join-Path $BackendDir '.venv\Scripts\python.exe'
$LauncherDir = Join-Path $BackendDir 'data\launcher'
$PidFile = Join-Path $LauncherDir 'pids.json'
$BackendOut = Join-Path $LauncherDir 'backend.out.log'
$BackendErr = Join-Path $LauncherDir 'backend.err.log'
$FrontendOut = Join-Path $LauncherDir 'frontend.out.log'
$FrontendErr = Join-Path $LauncherDir 'frontend.err.log'
$LogFiles = @($BackendOut, $BackendErr, $FrontendOut, $FrontendErr)

$BackendPort = 8000
$FrontendPort = 5173
$ReadyTimeoutSec = 90

# 本脚本亲自启动的 PID。注意**不能**把变量命名为 $pid：
# PowerShell 里 $PID 是只读自动变量（当前进程 ID），赋值会直接抛异常。
$script:StartedPids = @()

function Write-Step([string]$Text) { Write-Host " $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host " $Text" -ForegroundColor Green }

function Fail([string]$Text, [string[]]$Hints) {
    Write-Host ''
    Write-Host " [X] $Text" -ForegroundColor Red
    foreach ($Hint in $Hints) { Write-Host "     $Hint" -ForegroundColor DarkGray }
    Write-Host ''
    if (-not $NoPause -and -not $DryRun) { Read-Host ' 按回车键关闭此窗口' | Out-Null }
    exit 1
}

function Test-PortOpen([int]$Port) {
    # 用裸 TCP 探测本机端口；每个地址 500ms 连不上就当作该地址不通。
    # 不用 Invoke-WebRequest：本机 http_proxy 会把 localhost 请求送到代理，返回 502，
    # 于是「服务明明起来了却永远检测不到」。这里只建 TCP 连接，不经任何代理栈。
    #
    # 三个地址族都要试：vite 默认绑 ::1（IPv6 localhost），只探 127.0.0.1 会
    # 「前端日志明明写着 ready，检测却永远超时」—— 仓库文档记过这个坑。
    # 循环变量不能叫 $Host：PowerShell 里 $Host 是只读自动变量（宿主对象）。
    $Targets = @()
    try { $Targets += [System.Net.Dns]::GetHostAddresses('localhost') } catch { }
    $Targets += [System.Net.IPAddress]::Parse('127.0.0.1')
    $Targets += [System.Net.IPAddress]::Parse('::1')
    foreach ($Ip in $Targets) {
        # 必须按目标的 AddressFamily 建 socket：不带参数的 TcpClient 是 IPv4-only，
        # 拿它连 ::1 会抛 AddressFamilyNotSupported —— 于是「vite 日志明明 ready，
        # 端口探测却永远超时」。这是本函数第二个容易写错的地方（第一个是 $Host）。
        $client = New-Object System.Net.Sockets.TcpClient($Ip.AddressFamily)
        try {
            $task = $client.ConnectAsync($Ip, $Port)
            if ($task.Wait(500) -and $client.Connected) { return $true }
        } catch {
            # 这个地址不通，继续试下一个
        } finally {
            $client.Close()
        }
    }
    return $false
}

function Wait-ServiceReady {
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
    $backendOk = $false
    $frontendOk = $false
    Write-Host ' 等待服务就绪 ' -NoNewline -ForegroundColor DarkGray
    while ((Get-Date) -lt $deadline) {
        if (-not $backendOk) { $backendOk = Test-PortOpen $BackendPort }
        if (-not $frontendOk) { $frontendOk = Test-PortOpen $FrontendPort }
        if ($backendOk -and $frontendOk) {
            Write-Host ' OK' -ForegroundColor Green
            return $true
        }
        Write-Host '.' -NoNewline -ForegroundColor DarkGray
        Start-Sleep -Milliseconds 500
    }
    Write-Host ' 超时' -ForegroundColor Red
    return $false
}

function Stop-ProcessTree([int]$ProcId) {
    if (-not $ProcId -or $ProcId -le 0) { return }
    # taskkill 在「进程已退出 / 不存在」时会往 stderr 写一行。本脚本设了
    # $ErrorActionPreference='Stop'，Windows PowerShell 5.1 会把原生命令的 stderr
    # 当作终止性错误（NativeCommandError），导致清理中途 abort：
    # pids.json 删不掉、退出码变 1、还可能漏杀后面的进程。
    # 用 cmd.exe 在子进程内 >nul 2>&1 吞掉输出，PowerShell 根本收不到错误记录。
    try { & cmd.exe /c "taskkill /PID $ProcId /T /F >nul 2>&1" } catch { }
}

function Stop-LaunchedServices {
    # 按 /T 杀整棵进程树：--reload 会派生 uvicorn 子进程，npm run dev 会派生 node，
    # 只杀父进程会留下孤儿继续占端口。
    foreach ($ProcId in @($script:StartedPids)) {
        Stop-ProcessTree ([int]$ProcId)
    }
    $script:StartedPids = @()
    if (Test-Path $PidFile) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
}

# 尽力清理：用户直接点 X 关窗口时，PowerShell 退出事件仍会触发一次。
# 这是「尽力」而不是保证 —— 进程被强杀时不会触发，兜底靠 3. 停止网页端(Windows).bat。
Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    if ($script:KeepServicesOnExit) { return }
    foreach ($ProcId in @($script:StartedPids)) {
        if ($ProcId -and $ProcId -gt 0) {
            try { & cmd.exe /c "taskkill /PID $ProcId /T /F >nul 2>&1" } catch { }
        }
    }
} | Out-Null

Write-Host ''
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host '   多模态沉浸式智能助教  ·  一键启动' -ForegroundColor White
Write-Host ' ============================================================' -ForegroundColor DarkCyan
if ($DryRun) { Write-Host '   [DryRun] 只检查，不启动、不写文件、不等待' -ForegroundColor Yellow }
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
Write-Ok '[0/5] Python / Node.js 检查通过'

# ---------- 1. 后端运行环境 ----------
if (-not (Test-Path $VenvPython)) {
    if ($DryRun) {
        Write-Step '[1/5] 将创建 Python 虚拟环境并安装后端依赖（DryRun 未执行）'
    } else {
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
    }
} else {
    Write-Ok '[1/5] 后端虚拟环境已就绪'
}

# ---------- 2. 配置文件 ----------
$EnvFile = Join-Path $BackendDir '.env'
if (-not (Test-Path $EnvFile)) {
    if ($DryRun) {
        Write-Step '[2/5] 将从 .env.example 创建 backend\.env（DryRun 未执行）'
    } else {
        Copy-Item (Join-Path $BackendDir '.env.example') $EnvFile -ErrorAction SilentlyContinue
        Write-Step '[2/5] 已创建 backend\.env（当前为演示模式，未配置真实模型）'
        Write-Host '       要接入真实模型：编辑该文件并填写 LLM_API_KEY' -ForegroundColor DarkGray
    }
} else {
    Write-Ok '[2/5] backend\.env 已存在'
}

# ---------- 3. 前端依赖 ----------
if (-not (Test-Path (Join-Path $FrontendDir 'node_modules'))) {
    if ($DryRun) {
        Write-Step '[3/5] 将安装前端依赖（DryRun 未执行）'
    } else {
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
    }
} else {
    Write-Ok '[3/5] 前端依赖已就绪'
}

# ---------- 4. 示例教材 ----------
$SamplePdf = Join-Path $BackendDir 'data\sample_textbook.pdf'
if (-not (Test-Path $SamplePdf)) {
    if ($DryRun) {
        Write-Step '[4/5] 将生成示例教材（DryRun 未执行）'
    } else {
        Write-Step '[4/5] 生成示例教材（113 页）...'
        Push-Location $BackendDir
        & $VenvPython 'scripts\make_sample_pdf.py' | Out-Null
        Pop-Location
        Write-Ok '[4/5] 示例教材已生成'
    }
} else {
    Write-Ok '[4/5] 示例教材已存在'
}

# ---------- 端口状态（DryRun 与正式流程都检测） ----------
$BackendPortOpen = Test-PortOpen $BackendPort
$FrontendPortOpen = Test-PortOpen $FrontendPort
Write-Host ''
Write-Host " 端口检测：后端 $BackendPortOpen / 前端 $FrontendPortOpen" -ForegroundColor DarkGray

if ($DryRun) {
    Write-Host ''
    Write-Host ' ---------- DryRun：将要执行的动作 ----------' -ForegroundColor Yellow
    if (-not $BackendPortOpen) {
        Write-Host "  启动后端  : $VenvPython -m uvicorn app.main:app --reload --port $BackendPort (隐藏窗口)" -ForegroundColor Gray
    } else {
        Write-Host "  后端 $BackendPort 端口已占用：跳过启动（不重复起服务）" -ForegroundColor Gray
    }
    if (-not $FrontendPortOpen) {
        Write-Host "  启动前端  : cmd.exe /c npm run dev (隐藏窗口)" -ForegroundColor Gray
    } else {
        Write-Host "  前端 $FrontendPort 端口已占用：跳过启动（不重复起服务）" -ForegroundColor Gray
    }
    Write-Host "  就绪检测  : TCP 127.0.0.1:$BackendPort + 127.0.0.1:$FrontendPort，每 0.5s，最多 $ReadyTimeoutSec s" -ForegroundColor Gray
    if ($NoBrowser) {
        Write-Host '  打开浏览器: 否（-NoBrowser）' -ForegroundColor Gray
    } else {
        Write-Host "  打开浏览器: $TargetUrl" -ForegroundColor Gray
    }
    Write-Host "  日志目录  : $LauncherDir" -ForegroundColor Gray
    Write-Host ''
    Write-Host ' DryRun 结束：未启动任何服务、未开浏览器、未写任何文件。' -ForegroundColor Green
    exit 0
}

# ---------- 5. 启动前后端（单窗口，服务全部隐藏） ----------
Write-Host ''
Write-Step '[5/5] 启动服务（后台无窗口运行）...'

if (-not (Test-Path $LauncherDir)) {
    New-Item -ItemType Directory -Path $LauncherDir -Force | Out-Null
}
# 清空上一次的日志，避免排错时把旧错误当成新问题
foreach ($LogFile in $LogFiles) {
    if (Test-Path $LogFile) { Remove-Item $LogFile -Force -ErrorAction SilentlyContinue }
}

if ($BackendPortOpen -or $FrontendPortOpen) {
    Write-Host ' 检测到端口已占用：已运行的服务不会被重复启动。' -ForegroundColor Yellow
}

# 不要再起 powershell.exe 中间层：直接起 python / cmd，PID 树更干净。
# -RedirectStandardOutput 与 -RedirectStandardError 必须指向**不同**文件，
# 指向同一个会抛「无法打开同一个文件进行两次写入」。
if (-not $BackendPortOpen) {
    $BackendProc = Start-Process -FilePath $VenvPython `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--reload', '--port', "$BackendPort") `
        -WorkingDirectory $BackendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $BackendOut -RedirectStandardError $BackendErr
    $script:StartedPids += $BackendProc.Id
    Write-Ok " 后端已启动 (PID $($BackendProc.Id))"
} else {
    Write-Host ' 后端已在运行，跳过启动' -ForegroundColor DarkGray
}

if (-not $FrontendPortOpen) {
    $FrontendProc = Start-Process -FilePath 'cmd.exe' `
        -ArgumentList @('/c', 'npm', 'run', 'dev') `
        -WorkingDirectory $FrontendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $FrontendOut -RedirectStandardError $FrontendErr
    $script:StartedPids += $FrontendProc.Id
    Write-Ok " 前端已启动 (PID $($FrontendProc.Id))"
} else {
    Write-Host ' 前端已在运行，跳过启动' -ForegroundColor DarkGray
}

# 记录本脚本亲自启动的 PID。已在运行、非本脚本启动的服务绝不写进来，
# 否则按回车 / 兜底脚本会把别人的服务一起杀掉。
@{
    pids = @($script:StartedPids)
    updated_at = (Get-Date).ToString('o')
    target_url = $TargetUrl
} | ConvertTo-Json | Set-Content -Path $PidFile -Encoding UTF8

# ---------- 就绪检测（取代固定 Start-Sleep 10） ----------
if (-not (Wait-ServiceReady)) {
    Write-Host ''
    Write-Host ' [X] 服务未就绪（90 秒内两个端口没有同时可连）。' -ForegroundColor Red
    Write-Host '     请先查看四个日志文件：' -ForegroundColor Red
    foreach ($LogFile in $LogFiles) { Write-Host "       $LogFile" -ForegroundColor DarkGray }
    Write-Host '     常见原因：端口被别的程序占用 / 依赖没装全 / .env 配错。' -ForegroundColor DarkGray
    Stop-LaunchedServices
    exit 1
}

if (-not $NoBrowser) {
    Start-Process $TargetUrl | Out-Null
}

Write-Host ''
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host '   启动完成（只保留这一个窗口）' -ForegroundColor White
Write-Host ''
Write-Host "   前端界面   $TargetUrl" -ForegroundColor Gray
Write-Host '   接口文档   http://127.0.0.1:8000/docs' -ForegroundColor Gray
Write-Host '   健康检查   http://127.0.0.1:8000/api/health' -ForegroundColor Gray
Write-Host "   日志目录   $LauncherDir" -ForegroundColor Gray
Write-Host '              backend.out.log / backend.err.log' -ForegroundColor DarkGray
Write-Host '              frontend.out.log / frontend.err.log' -ForegroundColor DarkGray
Write-Host '   安装为桌面应用：在 Chrome/Edge 地址栏右侧点「安装」图标' -ForegroundColor Gray
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host ''

if ($NoPause) {
    Write-Host ' -NoPause：不等待回车，且**不会**清理子进程。' -ForegroundColor Yellow
    Write-Host '   服务有意留在后台供自动化使用；关掉这个窗口不会停止服务。' -ForegroundColor Yellow
    Write-Host '   需要停止时请运行 3. 停止网页端(Windows).bat' -ForegroundColor Yellow
    exit 0
}

Write-Host '   按回车键停止服务并关闭（会杀掉整棵进程树）' -ForegroundColor White
Write-Host '   若直接点 X 关窗口，请再运行 3. 停止网页端(Windows).bat 兜底清理' -ForegroundColor DarkGray
Write-Host ''
Read-Host ' 按回车键停止服务并关闭' | Out-Null
Stop-LaunchedServices
Write-Ok ' 服务已停止，PID 文件已清理。'
