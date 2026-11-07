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

    T02：端口预检升级为「空闲 / 本项目已运行 / 外部占用」三态（含 /api/health
    身份校验），杜绝二次双击把 pids.json 覆写成空列表（C8）；pids.json 仅在本轮
    真的启动了进程时才原子写入，并追加进程身份明细（P1-3）；就绪轮询加入子进程
    早退与 err.log 致命错误检测，失败时 tail 日志而不是等满 90 秒。

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

.PARAMETER ForceJobFallback
    纯测试开关：用于稳定复现 Job 能力探测的降级路径。T01 只预留参数、不改变默认行为；
    逻辑在 T05 实现，与 -DryRun 组合时同样保持无副作用。

.NOTES
    本文件必须以 UTF-8 with BOM 保存。存成「UTF-8 无 BOM」时 Windows PowerShell 5.1
    会按 ANSI 代码页解读，中文提示全变乱码；存成 GBK 则英文 Windows 上乱码。
#>
param(
    [switch]$NoPause,
    [switch]$NoBrowser,
    [string]$TargetUrl = 'http://localhost:5173',
    [switch]$DryRun,
    [switch]$ForceJobFallback
)

$ErrorActionPreference = 'Stop'

# 仅加载 .NET 程序集，供下面 /api/health 身份校验构造 HttpClient 使用。
# 这与 T05 的「Add-Type 编译 C# 能力探测」无关；PS 5.1 默认并未加载 System.Net.Http。
try { Add-Type -AssemblyName System.Net.Http -ErrorAction Stop } catch { }

# -NoPause 的语义是「不等待回车、不清理子进程」，服务有意留在后台。
# 退出事件里必须据此跳过清理，否则自动化模式刚起来的服务会被自己杀掉。
$script:KeepServicesOnExit = [bool]$NoPause

# ---------- T01 基础设施：中英资源表 / Msg() / 探测框架 / 字段冻结 ----------

# 所有用户可见文案集中在此，Msg() 按 $script:ConsoleCapability.MsgLang 取 zh / en。
# 新增任何用户可见文案必须同时提供 zh / en 两条；en 分支必须纯 ASCII。
$Messages = @{
    zh = @{
        'Banner.Title'        = '多模态沉浸式智能助教  ·  一键启动'
        'Banner.DryRun'       = '[DryRun] 只检查，不启动、不写文件、不等待'
        'DryRun.StatusHeader' = '---------- DryRun：四类状态输出 ----------'
        'Env.PythonMissing'   = '未找到 Python。'
        'Env.NodeMissing'     = '未找到 Node.js。'
        'Step.StartServices'  = '[5/5] 启动服务（后台无窗口运行）...'
        'Ready.Waiting'       = '等待服务就绪'
        'Ready.Timeout'       = '服务未就绪（90 秒内两个端口没有同时可连）。'
        'Done.Title'          = '启动完成（只保留这一个窗口）'
        'Stop.PressEnter'     = '按回车键停止服务并关闭（会杀掉整棵进程树）'
        'Stop.ReadHost'       = '按回车键停止服务并关闭'
        'Stop.Done'           = '服务已停止，PID 文件已清理。'
        'Fail.PressEnter'     = '按回车键关闭此窗口'
        'Port.StateHeader'    = '端口状态：后端 {0} / 前端 {1}（Ours=本项目已运行 / Foreign=外部占用 / Free=空闲）'
        'Port.Foreign'        = '端口 {0} 已被非本项目进程占用（身份校验未通过）。不会跳过启动，也不会显示启动完成。'
        'Port.ForeignHint'    = '请执行 netstat -ano | findstr :{0} 找到占用者 PID，结束后重新运行。'
        'Port.AlreadyRunning' = '本项目的服务已在运行：不会重复启动，也绝不会覆写 pids.json。如需重启请先运行停止脚本。'
        'Ready.EarlyExit'     = '[X] 服务进程提前退出或日志出现致命错误，已停止等待。'
        'Ready.ErrTail'       = '----- {0} 最后 {1} 行 -----'
        'PidFile.Skipped'     = '本轮没有启动任何服务：不写 pids.json，保留原有记录。'
    }
    en = @{
        'Banner.Title'        = 'Multimodal Interactive Edu Agent  -  One-click Launcher'
        'Banner.DryRun'       = '[DryRun] Check only; no start, no writes, no wait'
        'DryRun.StatusHeader' = '---------- DryRun: four status categories ----------'
        'Env.PythonMissing'   = 'Python not found.'
        'Env.NodeMissing'     = 'Node.js not found.'
        'Step.StartServices'  = '[5/5] Starting services (hidden, no window)...'
        'Ready.Waiting'       = 'Waiting for services'
        'Ready.Timeout'       = 'Services not ready within 90s.'
        'Done.Title'          = 'Started (this is the only window)'
        'Stop.PressEnter'     = 'Press ENTER to stop all services and close'
        'Stop.ReadHost'       = 'Press ENTER to stop and close'
        'Stop.Done'           = 'Services stopped; PID file removed.'
        'Fail.PressEnter'     = 'Press ENTER to close this window'
        'Port.StateHeader'    = 'Port states: backend={0} / frontend={1} (Ours=project / Foreign=external / Free=idle)'
        'Port.Foreign'        = 'Port {0} is occupied by a non-project process (identity check failed). No silent skip, no false Started banner.'
        'Port.ForeignHint'    = 'Run: netstat -ano | findstr :{0}  to find the owner PID, stop it, then retry.'
        'Port.AlreadyRunning' = 'Project services are already running: no duplicate start and pids.json is left untouched. Run the stop script first to restart.'
        'Ready.EarlyExit'     = '[X] A service exited early or a fatal error appeared in its log; wait aborted.'
        'Ready.ErrTail'       = '----- {0} : last {1} lines -----'
        'PidFile.Skipped'     = 'No service started this run: pids.json not written, existing record preserved.'
    }
}

function Msg([string]$Key) {
    # 按能力对象给出的语言取文案；缺键或缺语言时安全回退，绝不抛异常。
    $lang = 'zh'
    if ($null -ne $script:ConsoleCapability) { $lang = $script:ConsoleCapability.MsgLang }
    if (-not $Messages.ContainsKey($lang)) { $lang = 'zh' }
    $table = $Messages[$lang]
    if (-not $table.ContainsKey($Key)) { return $Key }
    return [string]$table[$Key]
}

function Get-ConsoleTextCapability {
    # T01 骨架：返回能力对象初值，字段形状与 §5.1 一致。
    # 初值取「保守不变」：DoChcp=true 保持既有 chcp 65001，MsgLang=zh 保持既有中文提示。
    # 三处注册表只读（ACP/OEMCP、FaceName、FontLink\SystemLink）与六档分支在 T03 实现。
    return [pscustomobject]@{
        ACP             = 'unknown'
        OEMCP           = 'unknown'
        FaceName        = 'unknown'
        HasFontLink     = $false
        HasCjkCoverage  = $false
        Branch          = 'skeleton (T03)'
        DoChcp          = $true
        OutputEncoding  = [System.Text.Encoding]::UTF8
        MsgLang         = 'zh'
    }
}

# 字段结构在此冻结（§3.3 十字段）；T05 只回填真实值，不改变调用点。
$script:LauncherCapability = [pscustomobject]@{
    AddTypeOk          = $false
    InJob              = $false
    ParentJobBreakaway = $false
    ParentJobUnknown   = $false
    CreateJobOk        = $false
    KillFlagApplied    = $false
    AssignProbeOk      = $false
    KillOnClose        = $false
    FallbackReason     = 'deferred (Job capability is implemented in T05)'
    TrioEnabled        = $true
}

function Get-JobCapability {
    # T01 占位：S1–S4 的真实能力探测在 T05 实现；此处只返回冻结的初值对象。
    return $script:LauncherCapability
}

$script:ConsoleCapability = Get-ConsoleTextCapability
$cap = $script:ConsoleCapability

# 控制台代码页按能力对象条件化执行；T01 初值 DoChcp=true，行为与改动前一致。
try {
    if ($cap.DoChcp) {
        chcp 65001 > $null
    }
    [Console]::OutputEncoding = $cap.OutputEncoding
    $OutputEncoding = $cap.OutputEncoding
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

# 后端端口可被外部注入（T04 / P1-6）：设了 EDU_BACKEND_PORT 就用它，
# 否则保持 8000，行为与改造前完全一致。**前端端口不参与**——5173 是 PWA 的
# origin 组成部分，一变已安装的快捷方式就失效，所以前端用 --strictPort 钉死。
$BackendPort = if ($env:EDU_BACKEND_PORT) { [int]$env:EDU_BACKEND_PORT } else { 8000 }
# 让子进程（vite）继承同一端口，proxy target 由 vite.config.ts 读取该变量拼出。
$env:EDU_BACKEND_PORT = "$BackendPort"
$FrontendPort = 5173
$ReadyTimeoutSec = 90

# 本脚本亲自启动的 PID。注意**不能**把变量命名为 $pid：
# PowerShell 里 $PID 是只读自动变量（当前进程 ID），赋值会直接抛异常。
$script:StartedPids = @()
# P0-5：保留 Start-Process -PassThru 的对象，就绪轮询里要查 HasExited。
$script:BackendProc = $null
$script:FrontendProc = $null
# 早失败时写入诊断原因，供主流程打印；'timeout' 表示确实是等满了 90 秒。
$script:ReadyFailureReason = ''

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

function Test-BackendIdentity([int]$Port) {
    <# P0-2：端口「能连上」不等于「是我们的服务」。

       显式禁用代理后请求 /api/health，只有 HTTP 200 且响应体 JSON 的 code == "OK"
       才认定占用者是本项目后端（backend/app/main.py 的 ApiResponse.ok 契约）。
       UseProxy=$false + Proxy=$null 是硬要求：本机 http_proxy 会把 127.0.0.1 请求
       送进代理返回 502，若不加会出现「明明是我们的服务却被判成外部占用」。
       任何异常（超时 / 非 JSON / 类型未加载）都保守返回 $false，交给三态逻辑报错。
    #>
    $client = $null
    try {
        $handler = New-Object System.Net.Http.HttpClientHandler
        $handler.UseProxy = $false
        $handler.Proxy = $null
        $client = New-Object System.Net.Http.HttpClient($handler)
        $client.Timeout = [TimeSpan]::FromSeconds(3)
        $response = $client.GetAsync("http://127.0.0.1:$Port/api/health").GetAwaiter().GetResult()
        try {
            if ([int]$response.StatusCode -ne 200) { return $false }
            $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            if ([string]::IsNullOrWhiteSpace($body)) { return $false }
            $payload = $body | ConvertFrom-Json -ErrorAction Stop
            return ($null -ne $payload -and [string]$payload.code -eq 'OK')
        } finally {
            if ($null -ne $response) { $response.Dispose() }
        }
    } catch {
        return $false
    } finally {
        if ($null -ne $client) { $client.Dispose() }
    }
}

function Test-FrontendIdentity([int]$Port) {
    # P0-1：5173 能连上时，用「首页 200 + Vite 特征」区分本项目 vite 与外部程序。
    # 与后端一样显式禁代理，避免 http_proxy 造成 502 假阴性。
    # 只读 GET，无副作用；超时/异常一律保守判为外部占用。
    $client = $null
    try {
        $handler = New-Object System.Net.Http.HttpClientHandler
        $handler.UseProxy = $false
        $handler.Proxy = $null
        $client = New-Object System.Net.Http.HttpClient($handler)
        $client.Timeout = [TimeSpan]::FromSeconds(3)
        $response = $client.GetAsync("http://127.0.0.1:$Port/").GetAwaiter().GetResult()
        try {
            if ([int]$response.StatusCode -ne 200) { return $false }
            $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            if ([string]::IsNullOrWhiteSpace($body)) { return $false }
            return ($body -match '/@vite/client' -or $body -match 'vite')
        } finally {
            if ($null -ne $response) { $response.Dispose() }
        }
    } catch {
        return $false
    } finally {
        if ($null -ne $client) { $client.Dispose() }
    }
}

function Get-PortState([int]$Port, [string]$Role) {
    # P0-1 / C8 三态：Free（空闲）/ Ours（本项目已运行）/ Foreign（外部占用）。
    # 旧实现只问「TCP 能不能连上」，于是二次双击两边都「跳过启动」，把 pids.json
    # 覆写成空列表并显示「启动完成」，从此永久失去停止能力。这里必须先做身份校验。
    if (-not (Test-PortOpen $Port)) { return 'Free' }
    if ($Role -eq 'backend') {
        if (Test-BackendIdentity $Port) { return 'Ours' }
        return 'Foreign'
    }
    if (Test-FrontendIdentity $Port) { return 'Ours' }
    return 'Foreign'
}

function Get-FatalLogLine([string]$Path) {
    # P0-5：err.log 末尾 20 行里出现 Traceback / ERROR 即视为致命信号。
    # -match 在 PowerShell 中默认不区分大小写，Vite 的 "Error: Port ..." 也能命中。
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $tail = @(Get-Content -LiteralPath $Path -Tail 20 -ErrorAction Stop)
        foreach ($line in $tail) {
            if ($line -match 'Traceback|ERROR') { return [string]$line }
        }
    } catch {
        # 日志被独占写或刚被清空时读不到：不是致命信号，继续轮询
    }
    return $null
}

function Write-ErrLogTail([string]$Path) {
    # 失败时把 err.log 末尾 20 行直接打到屏幕，用户不必再去翻四个日志文件。
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
        $tail = @(Get-Content -LiteralPath $Path -Tail 20 -ErrorAction Stop)
    } catch {
        return
    }
    if ($tail.Count -eq 0) { return }
    Write-Host ''
    Write-Host (' ' + ((Msg 'Ready.ErrTail') -f (Split-Path -Leaf $Path), $tail.Count)) -ForegroundColor Red
    foreach ($line in $tail) { Write-Host "     $line" -ForegroundColor DarkGray }
}

function Wait-ServiceReady {
    $script:ReadyFailureReason = ''
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
    $backendOk = $false
    $frontendOk = $false
    Write-Host (' ' + (Msg 'Ready.Waiting') + ' ') -NoNewline -ForegroundColor DarkGray
    while ((Get-Date) -lt $deadline) {
        # P0-5：本轮亲自启动的进程秒退、或日志出现致命错误 → 立即失败并 tail 日志，
        # 不再让用户盯满 90 秒。--reload 会派生 reloader + server 两层，父进程可能
        # 存活而子进程崩溃，所以 HasExited 与 err.log 关键字两个信号都要查。
        foreach ($watch in @(
            [pscustomobject]@{ Name = 'backend';  Proc = $script:BackendProc;  Log = $BackendErr },
            [pscustomobject]@{ Name = 'frontend'; Proc = $script:FrontendProc; Log = $FrontendErr }
        )) {
            if ($null -ne $watch.Proc) {
                try {
                    if ($watch.Proc.HasExited) {
                        $script:ReadyFailureReason = "$($watch.Name) 进程 (PID $($watch.Proc.Id)) 已提前退出"
                        Write-Host ' 失败' -ForegroundColor Red
                        return $false
                    }
                } catch {
                    # 权限不足等导致 HasExited 查询失败时不当作早失败信号
                }
            }
            $fatal = Get-FatalLogLine $watch.Log
            if ($null -ne $fatal) {
                $script:ReadyFailureReason = "$($watch.Name) 日志出现致命错误：$fatal"
                Write-Host ' 失败' -ForegroundColor Red
                return $false
            }
        }
        if (-not $backendOk) { $backendOk = Test-PortOpen $BackendPort }
        if (-not $frontendOk) { $frontendOk = Test-PortOpen $FrontendPort }
        if ($backendOk -and $frontendOk) {
            Write-Host ' OK' -ForegroundColor Green
            return $true
        }
        Write-Host '.' -NoNewline -ForegroundColor DarkGray
        Start-Sleep -Milliseconds 500
    }
    $script:ReadyFailureReason = 'timeout'
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

function Write-PidFile {
    <# P0-1 / P1-3：只有本轮真的启动了进程才写 pids.json，且必须原子写。

       三个旧字段（pids / updated_at / target_url）原样保留；新增可选 processes 明细，
       每项含 pid / process_name / start_time。按架构师裁定**不写 CommandLine**：
       ProcessName + StartTime.ToString('o') 已能唯一识别进程，且 Get-CimInstance
       在受限会话可能失败、命令行还可能含敏感参数。
       某个 PID 已退出 / Get-Process 抛异常时省略该条明细，不因此中断启动。
       先写同目录临时文件再 Move-Item -Force 覆盖，避免并发双击写出半截 JSON。
    #>
    if (@($script:StartedPids).Count -eq 0) {
        Write-Host (' ' + (Msg 'PidFile.Skipped')) -ForegroundColor DarkGray
        return
    }

    $processDetails = @()
    foreach ($ProcId in @($script:StartedPids)) {
        try {
            $proc = Get-Process -Id ([int]$ProcId) -ErrorAction Stop
            $processDetails += [pscustomobject]@{
                pid          = [int]$ProcId
                process_name = [string]$proc.ProcessName
                start_time   = $proc.StartTime.ToString('o')
            }
        } catch {
            # 进程已退出或取不到 StartTime：只省略明细，pids 仍照记
        }
    }

    $payload = [ordered]@{
        pids       = @($script:StartedPids)
        processes  = [object[]]$processDetails
        updated_at = (Get-Date).ToString('o')
        target_url = $TargetUrl
    }
    $json = $payload | ConvertTo-Json -Depth 4

    $tmpFile = Join-Path $LauncherDir ("pids.{0}.{1}.tmp" -f $PID, [guid]::NewGuid().ToString('N'))
    try {
        [System.IO.File]::WriteAllText($tmpFile, $json, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmpFile -Destination $PidFile -Force -ErrorAction Stop
    } finally {
        if (Test-Path -LiteralPath $tmpFile) {
            Remove-Item -LiteralPath $tmpFile -Force -ErrorAction SilentlyContinue
        }
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

# ---------- 端口三态（DryRun 与正式流程都检测） ----------
$BackendState = Get-PortState $BackendPort 'backend'
$FrontendState = Get-PortState $FrontendPort 'frontend'
Write-Host ''
Write-Host (' ' + ((Msg 'Port.StateHeader') -f $BackendState, $FrontendState)) -ForegroundColor DarkGray

if ($DryRun) {
    Write-Host ''
    Write-Host (' ' + (Msg 'DryRun.StatusHeader')) -ForegroundColor Yellow
    $jc  = Get-JobCapability
    $cap = $script:ConsoleCapability
    Write-Host (" Job capability : AddType={0}; InJob={1}; ParentJobBreakaway={2}; ParentJobUnknown={3}; CreateJob={4}; KillFlag={5}; AssignProbe={6}; StructSize=deferred(T05); KillOnClose={7}; Fallback={8}; TrioEnabled={9}" -f @(
        $(if ($jc.AddTypeOk) { 'OK' } else { 'deferred(T05)' }),
        $(if ($jc.InJob) { 'yes' } else { 'no' }),
        $(if ($jc.ParentJobBreakaway) { 'yes' } else { 'no' }),
        $(if ($jc.ParentJobUnknown) { 'yes' } else { 'no' }),
        $(if ($jc.CreateJobOk) { 'OK' } else { 'deferred(T05)' }),
        $(if ($jc.KillFlagApplied) { 'applied' } else { 'deferred(T05)' }),
        $(if ($jc.AssignProbeOk) { 'OK' } else { 'deferred(T05)' }),
        $(if ($jc.KillOnClose) { 'yes' } else { 'no' }),
        $jc.FallbackReason,
        $(if ($jc.TrioEnabled) { 'yes' } else { 'no' })
    )) -ForegroundColor Gray
    Write-Host (" Console env    : ACP={0}; OEMCP={1}; FaceName={2}; HasFontLink={3}; HasCjkCoverage={4}; Branch={5}; DoChcp={6}; MsgLang={7}" -f @(
        $cap.ACP,
        $cap.OEMCP,
        $cap.FaceName,
        $(if ($cap.HasFontLink) { 'yes' } else { 'no' }),
        $(if ($cap.HasCjkCoverage) { 'yes' } else { 'no' }),
        $cap.Branch,
        $(if ($cap.DoChcp) { 'yes' } else { 'no' }),
        $cap.MsgLang
    )) -ForegroundColor Gray
    Write-Host (" Ports          : backend={0}; frontend={1}; TargetUrl={2}" -f @(
        $BackendPort,
        $FrontendPort,
        $TargetUrl
    )) -ForegroundColor Gray
    Write-Host (" Legacy trio    : enabled={0}  (pids.json / stop-web.ps1 / PowerShell.Exiting / Enter-kill)" -f @(
        $(if ($jc.TrioEnabled) { 'yes' } else { 'no' })
    )) -ForegroundColor Gray
    # §7.1 的 PidFile schema 行归 T02；Job race 归 T05、Docs URL 归 T04，此处不预实现。
    Write-Host ' PidFile schema : v2 (pids/updated_at/target_url + optional processes[pid,process_name,start_time]); legacy=compat' -ForegroundColor Gray
    Write-Host ''
    Write-Host ' ---------- DryRun：将要执行的动作 ----------' -ForegroundColor Yellow
    if ($BackendState -eq 'Foreign' -or $FrontendState -eq 'Foreign') {
        $badPort = $BackendPort
        if ($BackendState -ne 'Foreign') { $badPort = $FrontendPort }
        Write-Host "  端口三态  : 端口 $badPort 被外部占用，正式流程将报错退出（DryRun 未执行）" -ForegroundColor Gray
    } elseif ($BackendState -eq 'Ours' -or $FrontendState -eq 'Ours') {
        Write-Host '  端口三态  : 本项目服务已在运行，正式流程将提示后退出：不启动、不写 pids.json（DryRun 未执行）' -ForegroundColor Gray
    } else {
        Write-Host "  启动后端  : $VenvPython -m uvicorn app.main:app --reload --port $BackendPort (隐藏窗口)" -ForegroundColor Gray
        Write-Host "  启动前端  : cmd.exe /c npm run dev -- --port $FrontendPort --strictPort (隐藏窗口)" -ForegroundColor Gray
    }
    Write-Host "  就绪检测  : TCP 127.0.0.1:$BackendPort + 127.0.0.1:$FrontendPort，每 0.5s，最多 $ReadyTimeoutSec s；进程早退 / err.log 致命错误立即失败" -ForegroundColor Gray
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

# ---------- 三态守卫（正式流程，启动任何进程之前） ----------
if ($BackendState -eq 'Foreign' -or $FrontendState -eq 'Foreign') {
    # C8/C2 的核心：8000 被外部占用时必须明确报错，绝不能「跳过启动」后照常显示
    # 「启动完成」并把 pids.json 覆写成空列表，否则用户将永久失去停止能力。
    $badPort = $BackendPort
    $reason = '后端 /api/health 未返回 code=OK'
    if ($BackendState -ne 'Foreign') {
        $badPort = $FrontendPort
        $reason = '前端首页未返回 Vite 特征'
    }
    Fail ((Msg 'Port.Foreign') -f $badPort) @(
        "身份校验失败：$reason",
        ((Msg 'Port.ForeignHint') -f $badPort),
        "netstat -ano | findstr :$badPort"
    )
}

if ($BackendState -eq 'Ours' -or $FrontendState -eq 'Ours') {
    # C8 第二态：本项目服务已在运行（含二次双击 / -NoPause 遗留）。
    # 既不重复启动，也绝不覆写 pids.json，更不显示「启动完成」。
    # 混合态（一个 Ours 一个 Free）同样按「已在运行」处理：只补拉一个服务会让
    # 新 pids.json 丢失旧服务的 PID，宁可提示用户先整体停止再重启。
    Write-Host ''
    Write-Host (' ' + (Msg 'Port.AlreadyRunning')) -ForegroundColor Yellow
    Write-Host '   pids.json 未被改写；不会显示「启动完成」，也不会打开浏览器。' -ForegroundColor DarkGray
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

# 不要再起 powershell.exe 中间层：直接起 python / cmd，PID 树更干净。
# -RedirectStandardOutput 与 -RedirectStandardError 必须指向**不同**文件，
# 指向同一个会抛「无法打开同一个文件进行两次写入」。
if ($BackendState -eq 'Free') {
    $script:BackendProc = Start-Process -FilePath $VenvPython `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--reload', '--port', "$BackendPort") `
        -WorkingDirectory $BackendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $BackendOut -RedirectStandardError $BackendErr
    $script:StartedPids += $script:BackendProc.Id
    Write-Ok " 后端已启动 (PID $($script:BackendProc.Id))"
} else {
    Write-Host ' 后端端口状态非空闲，跳过启动' -ForegroundColor DarkGray
}

if ($FrontendState -eq 'Free') {
    # P0-3：显式 --port + --strictPort，5173 被占时 vite 响亮失败并写进
    # frontend.err.log，而不是静默递增到 5174 让启动器打开错站。
    $script:FrontendProc = Start-Process -FilePath 'cmd.exe' `
        -ArgumentList @('/c', 'npm', 'run', 'dev', '--', '--port', "$FrontendPort", '--strictPort') `
        -WorkingDirectory $FrontendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $FrontendOut -RedirectStandardError $FrontendErr
    $script:StartedPids += $script:FrontendProc.Id
    Write-Ok " 前端已启动 (PID $($script:FrontendProc.Id))"
} else {
    Write-Host ' 前端端口状态非空闲，跳过启动' -ForegroundColor DarkGray
}

# 记录本脚本亲自启动的 PID。已在运行、非本脚本启动的服务绝不写进来，
# 否则按回车 / 兜底脚本会把别人的服务一起杀掉。
Write-PidFile

# ---------- 就绪检测（取代固定 Start-Sleep 10） ----------
if (-not (Wait-ServiceReady)) {
    Write-Host ''
    if ($script:ReadyFailureReason -eq 'timeout') {
        Write-Host (' [X] ' + (Msg 'Ready.Timeout')) -ForegroundColor Red
    } else {
        Write-Host (' [X] ' + (Msg 'Ready.EarlyExit')) -ForegroundColor Red
        Write-Host "     $script:ReadyFailureReason" -ForegroundColor Red
    }
    Write-Host '     请先查看四个日志文件：' -ForegroundColor Red
    foreach ($LogFile in $LogFiles) { Write-Host "       $LogFile" -ForegroundColor DarkGray }
    Write-Host '     ---- err.log 末尾 20 行 ----' -ForegroundColor Red
    foreach ($ErrLog in @($BackendErr, $FrontendErr)) { Write-ErrLogTail $ErrLog }
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
Write-Host "   接口文档   http://127.0.0.1:$BackendPort/api/docs" -ForegroundColor Gray
Write-Host "   健康检查   http://127.0.0.1:$BackendPort/api/health" -ForegroundColor Gray
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
