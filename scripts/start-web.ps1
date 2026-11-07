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

    T05：能力探测成功后用 Windows Job Object + KILL_ON_JOB_CLOSE 把前后端
    （含 --reload 的 reloader+server 两层与 npm->node->esbuild 链）纳入同一 job，
    启动器被强杀 / 点 X / 崩溃时由内核终止全部进程。任一步失败、-NoPause 或
    -ForceJobFallback 都完整退回三件套（pids.json / 停止脚本 / 退出事件 / 按回车）。
    降级前先清 LimitFlags 再 CloseHandle，绝不带着 KILL flag 直接关句柄。

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
    纯测试开关：强制走 Job 能力探测的降级路径（三件套完整启用、服务照常可用），
    用于不依赖真实机器策略地稳定复现验收。T05 实现：完全跳过 native 能力探测，
    因此即便与 -DryRun 组合也零副作用。不改变默认行为。

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
        'Env.PythonHint1'         = '请安装 Python 3.12+，安装时务必勾选 "Add python.exe to PATH"'
        'Env.PythonHint2'         = '下载：https://www.python.org/downloads/'
        'Env.NodeHint1'           = '请安装 Node.js 20 或更高版本'
        'Env.NodeHint2'           = '下载：https://nodejs.org/'
        'Fail.VenvCreate'         = '创建虚拟环境失败。'
        'Fail.VenvHint1'          = '请确认 Python 版本 >= 3.12：python --version'
        'Fail.VenvHint2'          = '如提示缺少 venv 模块，请重新安装 Python 并勾选全部组件'
        'Fail.BackendDeps'        = '后端依赖安装失败。常见原因：'
        'Fail.DepsHint1'          = '网络问题：& ''{0}'' -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend\requirements.txt'
        'Fail.DepsHint2'          = '缺少编译工具：安装 Visual Studio Build Tools'
        'Fail.DepsHint3'          = '公司代理：先设置 HTTP_PROXY / HTTPS_PROXY 环境变量'
        'Fail.Npm'                = 'npm install 失败。'
        'Fail.NpmHint1'           = '网络问题：npm config set registry https://registry.npmmirror.com'
        'Fail.NpmHint2'           = '然后重新运行本脚本'
        'Ready.ProcExited'        = '{0} 进程 (PID {1}) 已提前退出'
        'Ready.FatalLog'          = '{0} 日志出现致命错误：{1}'
        'Common.Failed'           = '失败'
        'Common.Timeout'          = '超时'
        'DryRun.ActionHeader'     = '---------- DryRun：将要执行的动作 ----------'
        'DryRun.PortForeign'      = '端口三态  : 端口 {0} 被外部占用，正式流程将报错退出（DryRun 未执行）'
        'DryRun.PortOurs'         = '端口三态  : 本项目服务已在运行，正式流程将提示后退出：不启动、不写 pids.json（DryRun 未执行）'
        'DryRun.LaunchBackend'    = '启动后端  : {0} (隐藏窗口)'
        'DryRun.LaunchFrontend'   = '启动前端  : {0} (隐藏窗口)'
        'DryRun.ReadyCheck'       = '就绪检测  : TCP {0} + {1}，每 0.5s，最多 {2} s；进程早退 / err.log 致命错误立即失败'
        'DryRun.NoBrowser'        = '打开浏览器: 否（-NoBrowser）'
        'DryRun.Browser'          = '打开浏览器: {0}'
        'DryRun.LogDir'           = '日志目录  : {0}'
        'DryRun.Done'             = 'DryRun 结束：未启动任何服务、未开浏览器、未写任何文件。'
        'Env.RealModelHint'       = '要接入真实模型：编辑该文件并填写 LLM_API_KEY'
        'Step.BackendBusy'        = '后端端口状态非空闲，跳过启动'
        'Step.FrontendBusy'       = '前端端口状态非空闲，跳过启动'
        'Done.FrontendUrl'        = '前端界面   {0}'
        'Done.ApiDocs'            = '接口文档   {0}'
        'Done.Health'             = '健康检查   {0}'
        'Done.LogDir'             = '日志目录   {0}'
        'Done.InstallPwa'         = '安装为桌面应用：在 Chrome/Edge 地址栏右侧点「安装」图标'
        'Stop.NoPause1'           = '-NoPause：不等待回车，且不会清理子进程。'
        'Stop.NoPause2'           = '服务有意留在后台供自动化使用；关掉这个窗口不会停止服务。'
        'Stop.NoPause3'           = '需要停止时请运行 3. 停止网页端(Windows).bat'
        'Stop.CloseHint'          = '若直接点 X 关窗口，请再运行 3. 停止网页端(Windows).bat 兜底清理'
        'Fail.LogFiles'           = '请先查看四个日志文件：'
        'Fail.ErrTailHeader'      = '---- err.log 末尾 20 行 ----'
        'Fail.CommonCauses'       = '常见原因：端口被别的程序占用 / 依赖没装全 / .env 配错。'
        'Port.OursNoWrite'        = 'pids.json 未被改写；不会显示「启动完成」，也不会打开浏览器。'
        'Step.EnvOk'              = '[0/5] Python / Node.js 检查通过'
        'Step.VenvCreateDry'      = '[1/5] 将创建 Python 虚拟环境并安装后端依赖（DryRun 未执行）'
        'Step.VenvCreate'         = '[1/5] 首次运行：创建 Python 虚拟环境...'
        'Step.VenvInstall'        = '[1/5] 安装后端依赖（约 1-2 分钟，请耐心等待）...'
        'Step.VenvDone'           = '[1/5] 后端依赖安装完成'
        'Step.VenvReady'          = '[1/5] 后端虚拟环境已就绪'
        'Step.EnvCreateDry'       = '[2/5] 将从 .env.example 创建 backend\.env（DryRun 未执行）'
        'Step.EnvCreated'         = '[2/5] 已创建 backend\.env（当前为演示模式，未配置真实模型）'
        'Step.EnvExists'          = '[2/5] backend\.env 已存在'
        'Step.NpmInstallDry'      = '[3/5] 将安装前端依赖（DryRun 未执行）'
        'Step.NpmInstall'         = '[3/5] 首次运行：安装前端依赖（约 1-3 分钟）...'
        'Step.NpmDone'            = '[3/5] 前端依赖安装完成'
        'Step.NpmReady'           = '[3/5] 前端依赖已就绪'
        'Step.SampleDry'          = '[4/5] 将生成示例教材（DryRun 未执行）'
        'Step.SampleGen'          = '[4/5] 生成示例教材（113 页）...'
        'Step.SampleDone'         = '[4/5] 示例教材已生成'
        'Step.SampleExists'       = '[4/5] 示例教材已存在'
        'Step.BackendStarted'     = ' 后端已启动 (PID {0})'
        'Step.FrontendStarted'    = ' 前端已启动 (PID {0})'
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
        'Job.Fallback'        = '本次未启用自动清理（原因：<R>），请在本窗口按回车停止服务后再关闭。'
        'Job.CloseStops'      = '启动完成。直接关闭本窗口即会自动停止全部服务。'
        'Job.NoPause'         = '-NoPause：服务常驻后台，请运行 3. 停止网页端(Windows).bat 停止。'
        'Job.RaceNotice'      = '已保留开发期热重载（--reload）；极端情况下早于首个进程加入任务前派生的孙进程可能残留，需实现后实测。'
    }
    en = @{
        'Env.PythonHint1'         = 'Install Python 3.12+ and make sure "Add python.exe to PATH" is checked'
        'Env.PythonHint2'         = 'Download: https://www.python.org/downloads/'
        'Env.NodeHint1'           = 'Install Node.js 20 or newer'
        'Env.NodeHint2'           = 'Download: https://nodejs.org/'
        'Fail.VenvCreate'         = 'Failed to create the virtual environment.'
        'Fail.VenvHint1'          = 'Check Python version >= 3.12: python --version'
        'Fail.VenvHint2'          = 'If the venv module is missing, reinstall Python with all components'
        'Fail.BackendDeps'        = 'Backend dependency install failed. Common causes:'
        'Fail.DepsHint1'          = 'Network: & ''{0}'' -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend\requirements.txt'
        'Fail.DepsHint2'          = 'Missing build tools: install Visual Studio Build Tools'
        'Fail.DepsHint3'          = 'Corporate proxy: set HTTP_PROXY / HTTPS_PROXY first'
        'Fail.Npm'                = 'npm install failed.'
        'Fail.NpmHint1'           = 'Network: npm config set registry https://registry.npmmirror.com'
        'Fail.NpmHint2'           = 'Then run this script again'
        'Ready.ProcExited'        = '{0} process (PID {1}) exited early'
        'Ready.FatalLog'          = '{0} log shows a fatal error: {1}'
        'Common.Failed'           = 'failed'
        'Common.Timeout'          = 'timed out'
        'DryRun.ActionHeader'     = '---------- DryRun: planned actions ----------'
        'DryRun.PortForeign'      = 'Port state : port {0} is held by an external process; the real run aborts with an error (DryRun made no change)'
        'DryRun.PortOurs'         = 'Port state : project services already running; the real run exits after a notice - no start, no pids.json write (DryRun made no change)'
        'DryRun.LaunchBackend'    = 'Start back : {0} (hidden window)'
        'DryRun.LaunchFrontend'   = 'Start front: {0} (hidden window)'
        'DryRun.ReadyCheck'       = 'Readiness  : TCP {0} + {1}, every 0.5s, up to {2}s; fail fast on early exit or fatal err.log'
        'DryRun.NoBrowser'        = 'Open browser: no (-NoBrowser)'
        'DryRun.Browser'          = 'Open browser: {0}'
        'DryRun.LogDir'           = 'Log dir    : {0}'
        'DryRun.Done'             = 'DryRun finished: no service started, no browser, no files written.'
        'Env.RealModelHint'       = 'To use a real model, edit that file and set LLM_API_KEY'
        'Step.BackendBusy'        = 'Backend port is not idle; skipping start'
        'Step.FrontendBusy'       = 'Frontend port is not idle; skipping start'
        'Done.FrontendUrl'        = 'Frontend   {0}'
        'Done.ApiDocs'            = 'API docs   {0}'
        'Done.Health'             = 'Health     {0}'
        'Done.LogDir'             = 'Log dir    {0}'
        'Done.InstallPwa'         = 'Install as desktop app: click the Install icon in the Chrome/Edge address bar'
        'Stop.NoPause1'           = '-NoPause: no Enter prompt and NO child-process cleanup.'
        'Stop.NoPause2'           = 'Services stay in background on purpose for automation; closing this window does NOT stop them.'
        'Stop.NoPause3'           = 'To stop them run: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-web.ps1'
        'Stop.CloseHint'          = 'If you close this window with X, run: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-web.ps1'
        'Fail.LogFiles'           = 'Check these four log files first:'
        'Fail.ErrTailHeader'      = '---- last 20 lines of err.log ----'
        'Fail.CommonCauses'       = 'Common causes: port taken by another program / missing deps / wrong .env.'
        'Port.OursNoWrite'        = 'pids.json left untouched; no Started banner and no browser will be opened.'
        'Step.EnvOk'              = '[0/5] Python / Node.js OK'
        'Step.VenvCreateDry'      = '[1/5] Would create venv and install backend deps (DryRun: skipped)'
        'Step.VenvCreate'         = '[1/5] First run: creating Python venv...'
        'Step.VenvInstall'        = '[1/5] Installing backend deps (~1-2 min, please wait)...'
        'Step.VenvDone'           = '[1/5] Backend deps installed'
        'Step.VenvReady'          = '[1/5] Backend venv ready'
        'Step.EnvCreateDry'       = '[2/5] Would create backend\.env from .env.example (DryRun: skipped)'
        'Step.EnvCreated'         = '[2/5] Created backend\.env (demo mode, no real model configured)'
        'Step.EnvExists'          = '[2/5] backend\.env already exists'
        'Step.NpmInstallDry'      = '[3/5] Would install frontend deps (DryRun: skipped)'
        'Step.NpmInstall'         = '[3/5] First run: installing frontend deps (~1-3 min)...'
        'Step.NpmDone'            = '[3/5] Frontend deps installed'
        'Step.NpmReady'           = '[3/5] Frontend deps ready'
        'Step.SampleDry'          = '[4/5] Would generate sample textbook (DryRun: skipped)'
        'Step.SampleGen'          = '[4/5] Generating sample textbook (113 pages)...'
        'Step.SampleDone'         = '[4/5] Sample textbook generated'
        'Step.SampleExists'       = '[4/5] Sample textbook already exists'
        'Step.BackendStarted'     = ' Backend started (PID {0})'
        'Step.FrontendStarted'    = ' Frontend started (PID {0})'
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
        'Job.Fallback'        = 'Auto-cleanup NOT enabled this run (<R>). Press ENTER here to stop services before closing.'
        'Job.CloseStops'      = 'Started. Closing this window stops ALL services automatically.'
        'Job.NoPause'         = '-NoPause: services stay in background. To stop them, run: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-web.ps1'
        'Job.RaceNotice'      = 'Dev hot-reload (--reload) kept; a grandchild forked before the first job assignment may rarely survive. Needs real-world verification.'
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
    <# T03：按「ACP + 控制台字体 CJK 覆盖」判定六档环境（设计 §5.1 / §5.2）。

       为什么不能只看系统语言：本机就是 zh-CN 但 ACP=65001、字体 Consolas 且
       FontLink\SystemLink 无任何 CJK 回退——此时 chcp 65001 是空操作，瓶颈
       100% 在字形缺失，中文必然显示为方块。所以判据必须是「代码页 + 字体覆盖」。

       零侵入：只读三处注册表，不写 HKCU、不改控制台字体、不改系统默认终端。
    #>
    $acp = 'unknown'; $oem = 'unknown'; $face = 'unknown'
    $hasFontLink = $false; $hasCjk = $false

    try {
        $nls = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Nls\CodePage' -ErrorAction Stop
        $acp = [string]$nls.ACP
        $oem = [string]$nls.OEMCP
    } catch { }

    try {
        $face = [string](Get-ItemProperty 'HKCU:\Console' -ErrorAction Stop).FaceName
    } catch { }
    if ([string]::IsNullOrWhiteSpace($face)) { $face = 'Consolas' }   # 最常见的零 CJK 回退字体

    $cjkPattern = 'SimSun|NSimSun|MingLiU|MSMINCHO|MS Gothic|MS PMincho|Yu Gothic|Malgun|Batang|YaHei|宋体|新宋体|明体|黑体|Sarasa|Noto.*CJK|Source Han'
    $faceIsCjk = $face -match $cjkPattern

    try {
        $link = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\FontLink\SystemLink' -ErrorAction Stop
        $prop = $link.PSObject.Properties | Where-Object { $_.Name -eq $face } | Select-Object -First 1
        if ($null -ne $prop) {
            $hasFontLink = $true
            $hasCjk = ([string]($prop.Value -join ' ')) -match $cjkPattern
        }
    } catch { }

    $hasCjkCoverage = $faceIsCjk -or $hasCjk

    # ---- 六档分支（§5.2）----
    $isCjkAcp = $acp -in @('936', '950', '932', '949')
    if ($isCjkAcp) {
        # ① / ①′：中文传统代码页。chcp 65001 会关掉 DBCS 渲染路径，反而不显示中文。
        $branch = if ($hasCjkCoverage) { '1 CJK-ACP' } else { '1b CJK-ACP no-font' }
        $doChcp = $false
        $enc = [System.Text.Encoding]::GetEncoding([int]$acp)
        $msgLang = 'zh'
    } elseif ($acp -eq '65001') {
        # ③ / ③′：已是 UTF-8（含 Windows「Beta: 使用 Unicode UTF-8」）。chcp 是空操作。
        $branch = if ($hasCjkCoverage) { '3 UTF-8-Beta' } else { '3b UTF-8-Beta no-font' }
        $doChcp = $false
        $enc = [System.Text.Encoding]::UTF8
        $msgLang = if ($hasCjkCoverage) { 'zh' } else { 'en' }
    } else {
        # ② / ②′：英文 / 西欧代码页。chcp 只解决「字节→码位」，字形仍需字体。
        # 一律英文，不赌用户装了 CJK 等宽字体。
        $branch = if ($hasCjkCoverage) { '2 Western cjk-font' } else { '2 Western' }
        $doChcp = $true
        $enc = [System.Text.Encoding]::UTF8
        $msgLang = 'en'
    }

    return [pscustomobject]@{
        ACP            = $acp
        OEMCP          = $oem
        FaceName       = $face
        HasFontLink    = $hasFontLink
        HasCjkCoverage = $hasCjkCoverage
        Branch         = $branch
        DoChcp         = $doChcp
        OutputEncoding = $enc
        MsgLang        = $msgLang
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
    # T01 冻结调用点；T05 只回填 $script:LauncherCapability 的真实值。
    return $script:LauncherCapability
}

# ---------- T05：Windows Job Object（决策 1 / P1-8，设计 §3.1 / §3.3） ----------
# 所有 P/Invoke 集中在这一个 here-string 里：不新增二进制、不落盘 DLL。
# 结构体必须 LayoutKind.Sequential + 平台默认 Pack（不要写 Pack=1，见 §3.2），
# 编译后立刻断言 Marshal.SizeOf：x64=144 / x86=112，不等即降级，
# 绝不带着错位结构继续调 SetInformationJobObject。
$script:NativeJobSource = @'
using System;
using System.Runtime.InteropServices;

namespace EduLauncher
{
    public static class JobConst
    {
        public const int  JobObjectBasicLimitInformation    = 2;
        public const int  JobObjectExtendedLimitInformation = 9;
        public const uint JOB_OBJECT_LIMIT_BREAKAWAY_OK        = 0x00000800;
        public const uint JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000;
        public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE   = 0x00002000;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long    PerProcessUserTimeLimit;
        public long    PerJobUserTimeLimit;
        public uint    LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint    ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint    PriorityClass;
        public uint    SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS  IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    public static class NativeJob
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool SetInformationJobObject(
            IntPtr hJob, int infoClass,
            ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, uint cbInfoLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool QueryInformationJobObject(
            IntPtr hJob, int infoClass,
            ref JOBOBJECT_BASIC_LIMIT_INFORMATION info, uint cbInfoLength,
            out uint lpReturnLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool IsProcessInJob(
            IntPtr hProcess, IntPtr hJob,
            [MarshalAs(UnmanagedType.Bool)] out bool Result);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll")]
        public static extern IntPtr GetCurrentProcess();

        // Three managed thin wrappers. Building the structs inside managed code
        // avoids PowerShell side effects on boxed value types; the P/Invoke
        // declarations above are verbatim from the design spec.
        public static int ExtendedLimitInfoSize()
        {
            return Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        }

        public static bool ApplyKillFlag(IntPtr hJob, bool enable)
        {
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION info =
                new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            info.BasicLimitInformation.LimitFlags = enable
                ? JobConst.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE : 0U;
            return SetInformationJobObject(
                hJob,
                JobConst.JobObjectExtendedLimitInformation,
                ref info,
                (uint)Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)));
        }

        public static bool TryQueryParentJobLimitFlags(out uint limitFlags)
        {
            limitFlags = 0;
            JOBOBJECT_BASIC_LIMIT_INFORMATION info =
                new JOBOBJECT_BASIC_LIMIT_INFORMATION();
            uint returned = 0;
            bool ok = QueryInformationJobObject(
                IntPtr.Zero,
                JobConst.JobObjectBasicLimitInformation,
                ref info,
                (uint)Marshal.SizeOf(typeof(JOBOBJECT_BASIC_LIMIT_INFORMATION)),
                out returned);
            if (ok) { limitFlags = info.LimitFlags; }
            return ok;
        }
    }
}
'@

# $script:JobHandle 由启动器持有到进程结束；不得提前换成会被 GC 回收的 SafeHandle。
$script:JobHandle = [IntPtr]::Zero
$script:JobStructSize = 0
$script:JobSizeAssert = 'skipped'
$script:JobUsable = $false
$script:TrioEnabled = $true
$script:ExitCleanupRegistered = $false

function Set-JobKillFlag([bool]$Enable) {
    # 降级安全不变量的核心动作：把 LimitFlags 清零（或置 KILL_ON_JOB_CLOSE）。
    # 这里绝不 CloseHandle —— job 内已 Assign 进程时那会立刻杀掉服务。
    if ($script:JobHandle -eq [IntPtr]::Zero) { return $false }
    try {
        return [bool][EduLauncher.NativeJob]::ApplyKillFlag($script:JobHandle, $Enable)
    } catch { return $false }
}

function Close-JobHandleSafely {
    # 任何失败路径统一走这里：先 SetInformationJobObject 把 LimitFlags 清零，
    # 再 CloseHandle。进程脱离 job 但继续运行，服务不丢（§3.3 降级安全不变量）。
    # 不修改能力字段：DryRun 需要如实保留 S4 的探测结果。
    if ($script:JobHandle -eq [IntPtr]::Zero) { return }
    try { [void](Set-JobKillFlag $false) } catch { }
    try { [void][EduLauncher.NativeJob]::CloseHandle($script:JobHandle) } catch { }
    $script:JobHandle = [IntPtr]::Zero
}

function Register-ExitCleanup {
    # 三件套之一：PowerShell.Exiting 尽力清理，由 $script:TrioEnabled 守卫。
    # 正常路径（Job 生效）不注册；降级 / -NoPause 路径注册且只注册一次。
    # DryRun 不注册：不启动任何服务，也不留进程内事件订阅。
    if ($DryRun) { return }
    if (-not $script:TrioEnabled) { return }
    if ($script:ExitCleanupRegistered) { return }
    try {
        Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
            if ($script:KeepServicesOnExit) { return }
            foreach ($ProcId in @($script:StartedPids)) {
                if ($ProcId -and $ProcId -gt 0) {
                    try { & cmd.exe /c "taskkill /PID $ProcId /T /F >nul 2>&1" } catch { }
                }
            }
        } | Out-Null
        $script:ExitCleanupRegistered = $true
    } catch { }
}

function Enable-LegacyTrio([string]$Reason) {
    # 完整退回三件套：pids.json / 停止脚本 / PowerShell.Exiting / 按回车杀树。
    # 代码从 T01 起一直存在，这里只把守卫翻回 true，不存在「先删后补」。
    $script:LauncherCapability.FallbackReason = $Reason
    $script:LauncherCapability.TrioEnabled = $true
    $script:LauncherCapability.KillOnClose = $false
    $script:LauncherCapability.KillFlagApplied = $false
    $script:TrioEnabled = $true
    $script:JobUsable = $false
    Close-JobHandleSafely
    Register-ExitCleanup
}

function Test-JobAssignProbe {
    # S5：在启动真实服务之前起一个 1 秒存活的探针进程，Assign 后用
    # IsProcessInJob 验证。失败时 job 内只有探针，降级零损失。
    $probe = $null
    try {
        $probe = Start-Process -FilePath 'cmd.exe' `
            -ArgumentList '/c ping -n 2 127.0.0.1 >nul' `
            -WindowStyle Hidden -PassThru
        if ($null -eq $probe) { return $false }
        $handle = $probe.Handle
        if (-not [EduLauncher.NativeJob]::AssignProcessToJobObject($script:JobHandle, $handle)) { return $false }
        $verified = $false
        $ok = [EduLauncher.NativeJob]::IsProcessInJob($handle, $script:JobHandle, [ref]$verified)
        return [bool]($ok -and $verified)
    } catch {
        return $false
    }
}

function Add-LaunchedProcessToJob([System.Diagnostics.Process]$Proc, [string]$Role) {
    # S6 主路径：每个 Start-Process -PassThru 返回后**立即** Assign + 验证，
    # 绝不等另一个服务、更不等就绪检测 / 写 pids.json 之后统一处理。
    if (-not $script:JobUsable) { return }
    if ($null -eq $Proc) { return }
    if ($script:JobHandle -eq [IntPtr]::Zero) { return }
    try {
        if ($Proc.HasExited) { return }
        $handle = $Proc.Handle
        if (-not [EduLauncher.NativeJob]::AssignProcessToJobObject($script:JobHandle, $handle)) {
            throw 'AssignProcessToJobObject returned false'
        }
        $verified = $false
        $ok = [EduLauncher.NativeJob]::IsProcessInJob($handle, $script:JobHandle, [ref]$verified)
        if (-not $ok -or -not $verified) { throw 'IsProcessInJob verification returned false' }
    } catch {
        Enable-LegacyTrio ("S6 immediate-Assign failed for {0} (err={1})" -f $Role, [Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
}

function Invoke-DescendantRescan {
    # 决议 6 的主缓解：--reload 保留，孙进程链是 reloader+server 两层 +
    # cmd -> npm -> node -> esbuild。CreateProcess -> Assign 窗口内派生的孙进程
    # 可能逃逸；用 Win32_Process 只读递归补挂 2 轮、间隔 200ms。不得省略、不得降为 1 轮。
    if (-not $script:JobUsable) { return }
    if ($script:JobHandle -eq [IntPtr]::Zero) { return }
    for ($round = 1; $round -le 2; $round++) {
        if ($round -gt 1) { Start-Sleep -Milliseconds 200 }
        try { $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop) } catch { return }
        $pending = New-Object System.Collections.ArrayList
        foreach ($rootId in @($script:StartedPids)) { [void]$pending.Add([int]$rootId) }
        $seen = @{}
        $descendants = @()
        $cursor = 0
        while ($cursor -lt $pending.Count) {
            $parentId = [int]$pending[$cursor]
            $cursor++
            foreach ($entry in $snapshot) {
                $childId = [int]$entry.ProcessId
                $key = "$childId"
                if (([int]$entry.ParentProcessId -eq $parentId) -and (-not $seen.ContainsKey($key))) {
                    $seen[$key] = $true
                    $descendants += $childId
                    [void]$pending.Add($childId)
                }
            }
        }
        foreach ($childId in $descendants) {
            try {
                $childProc = Get-Process -Id $childId -ErrorAction Stop
                $already = $false
                $ok = [EduLauncher.NativeJob]::IsProcessInJob($childProc.Handle, $script:JobHandle, [ref]$already)
                if ($ok -and -not $already) {
                    [void][EduLauncher.NativeJob]::AssignProcessToJobObject($script:JobHandle, $childProc.Handle)
                }
            } catch { }
        }
    }
}

function Initialize-JobCapability {
    <# S1–S4 能力探测（DryRun 与正式流程共用；§3.3 / §3.4） #>
    $cap = $script:LauncherCapability
    $cap.FallbackReason = 'none'
    $script:JobUsable = $false
    $script:JobSizeAssert = 'skipped'

    # -ForceJobFallback：纯测试开关，跳过全部 native 调用，强制完整降级。
    if ($ForceJobFallback) {
        $cap.FallbackReason = 'forced by -ForceJobFallback (test switch); native probe skipped'
        $script:TrioEnabled = $true
        $cap.TrioEnabled = $true
        Register-ExitCleanup
        return
    }

    # ---- S1：编译 ----
    $langMode = [string]$ExecutionContext.SessionState.LanguageMode
    if ($langMode -eq 'ConstrainedLanguage') {
        Enable-LegacyTrio 'Add-Type blocked (LanguageMode=ConstrainedLanguage)'
        return
    }
    if (-not ('EduLauncher.NativeJob' -as [type])) {
        try {
            Add-Type -TypeDefinition $script:NativeJobSource -ErrorAction Stop
        } catch {
            Enable-LegacyTrio ("Add-Type blocked (LanguageMode={0}, err={1})" -f $langMode, [string]$_.Exception.Message)
            return
        }
    }
    $cap.AddTypeOk = $true

    # 编译后立刻断言 x64 结构体布局（§3.1 第 2 条）：C# 侧
    # Marshal.SizeOf(typeof(...)) 与 PS 侧 Marshal.SizeOf(Type) 必须一致且等于期望值。
    $extType = 'EduLauncher.JOBOBJECT_EXTENDED_LIMIT_INFORMATION' -as [type]
    if ($null -eq $extType) {
        Enable-LegacyTrio 'struct type missing after compile'
        return
    }
    try {
        $script:JobStructSize = [int][EduLauncher.NativeJob]::ExtendedLimitInfoSize()
        # PS 5.1 会把 SizeOf($typeVar) 绑定到 object 重载并抛异常，必须显式 [type] 转换。
        $psSize = [int][Runtime.InteropServices.Marshal]::SizeOf([type]$extType)
    } catch {
        Enable-LegacyTrio 'Marshal.SizeOf probe failed'
        return
    }
    $expectedSize = if ([IntPtr]::Size -eq 8) { 144 } else { 112 }
    if (($script:JobStructSize -ne $expectedSize) -or ($psSize -ne $script:JobStructSize)) {
        $script:JobSizeAssert = 'FAIL'
        Enable-LegacyTrio ("struct size mismatch (got={0}/{1}, expected={2})" -f $script:JobStructSize, $psSize, $expectedSize)
        return
    }
    $script:JobSizeAssert = 'PASS'

    # ---- S2：宿主 job 判定 ----
    try {
        $inJob = $false
        $self = [EduLauncher.NativeJob]::GetCurrentProcess()
        $s2ok = [EduLauncher.NativeJob]::IsProcessInJob($self, [IntPtr]::Zero, [ref]$inJob)
        if (-not $s2ok) { throw 'IsProcessInJob returned false' }
        $cap.InJob = [bool]$inJob
        if ($cap.InJob) {
            # NULL 句柄语义在嵌套 Job 下需实测；查不到一律保守降级（§3.3 S2）。
            $parentFlags = [uint32]0
            $queried = $false
            try { $queried = [bool][EduLauncher.NativeJob]::TryQueryParentJobLimitFlags([ref]$parentFlags) } catch { $queried = $false }
            if (-not $queried) {
                $cap.ParentJobUnknown = $true
            } elseif ((($parentFlags -band 0x00001000) -ne 0) -or (($parentFlags -band 0x00000800) -ne 0)) {
                $cap.ParentJobBreakaway = $true
            }
            if ($cap.ParentJobBreakaway -or $cap.ParentJobUnknown) {
                Enable-LegacyTrio ("parent job breakaway/unknown (breakaway={0}, unknown={1})" -f $cap.ParentJobBreakaway, $cap.ParentJobUnknown)
                return
            }
        }
    } catch {
        Enable-LegacyTrio ("IsProcessInJob probe failed (err={0})" -f [Runtime.InteropServices.Marshal]::GetLastWin32Error())
        return
    }

    # ---- S3：CreateJobObject（未命名，句柄不继承给子进程） ----
    try { $script:JobHandle = [EduLauncher.NativeJob]::CreateJobObject([IntPtr]::Zero, $null) }
    catch { $script:JobHandle = [IntPtr]::Zero }
    if ($script:JobHandle -eq [IntPtr]::Zero) {
        Enable-LegacyTrio ("CreateJobObject failed (err={0})" -f [Runtime.InteropServices.Marshal]::GetLastWin32Error())
        return
    }
    $cap.CreateJobOk = $true

    # ---- S4：SetInformationJobObject ----
    $applyKill = (-not $NoPause)
    if (-not (Set-JobKillFlag $applyKill)) {
        Enable-LegacyTrio ("SetInformationJobObject failed (err={0})" -f [Runtime.InteropServices.Marshal]::GetLastWin32Error())
        return
    }
    $cap.KillFlagApplied = [bool]$applyKill
    $cap.KillOnClose = [bool]$applyKill

    # ---- DryRun：只做 S1–S4，句柄在同一进程内立即 CloseHandle；S5 不执行 ----
    if ($DryRun) {
        Close-JobHandleSafely
        if ($NoPause) {
            $cap.KillOnClose = $false
            $cap.FallbackReason = '-NoPause (KILL_ON_JOB_CLOSE intentionally not set)'
            $script:TrioEnabled = $true
            $cap.TrioEnabled = $true
        } else {
            $script:TrioEnabled = $false
            $cap.TrioEnabled = $false
        }
        return
    }

    if ($NoPause) {
        # §3.4 / §3.5：Job 仍创建但 LimitFlags=0；pids.json + 停止脚本必须可用。
        $cap.FallbackReason = '-NoPause (KILL_ON_JOB_CLOSE intentionally not set)'
        $script:TrioEnabled = $true
        $cap.TrioEnabled = $true
        return
    }

    # 真实非 -NoPause 启动：乐观置为休眠；S5 失败会翻回 true 并补注册退出事件。
    $script:TrioEnabled = $false
    $cap.TrioEnabled = $false
}

function Confirm-JobAssignProbe {
    # S5 放在「三态守卫通过之后、启动任何真实服务之前」，避免端口被占时白起探针。
    if ($DryRun -or $NoPause -or $ForceJobFallback) { return }
    if (-not $script:LauncherCapability.CreateJobOk) { return }
    if ($script:JobHandle -eq [IntPtr]::Zero) { return }
    if (-not (Test-JobAssignProbe)) {
        Enable-LegacyTrio ("Assign probe failed (S5, err={0})" -f [Runtime.InteropServices.Marshal]::GetLastWin32Error())
        return
    }
    $script:LauncherCapability.AssignProbeOk = $true
    $script:JobUsable = $true
    $script:TrioEnabled = $false
    $script:LauncherCapability.TrioEnabled = $false
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

function Get-DisplayPath([string]$Path) {
    <# 英文降级模式下把绝对路径相对化到项目根（设计 §5.1 末尾）。

       本项目的根目录是 `D:\学习目标_笔记\...`，本身含大量中文。若在英文降级时
       照常打印绝对路径，屏幕上仍会出现豆腐块 —— 文案全是 ASCII 也没用。
       项目内的 backend\... / frontend\... / 日志文件名本身都是 ASCII，
       去掉中文前缀即可；万一相对化后仍有非 ASCII，用 '?' 兜底而不是放任方块。
       中文模式下原样返回，用户看到完整路径更好定位。
    #>
    if ($script:ConsoleCapability.MsgLang -ne 'en') { return $Path }
    $rel = $Path
    if ($Path.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
        $rel = $Path.Substring($Root.Length).TrimStart('\', '/')
    }
    return ($rel -replace '[^\x20-\x7E]', '?')
}

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
                        $script:ReadyFailureReason = ((Msg 'Ready.ProcExited') -f $watch.Name, $watch.Proc.Id)
                        Write-Host (' ' + (Msg 'Common.Failed')) -ForegroundColor Red
                        return $false
                    }
                } catch {
                    # 权限不足等导致 HasExited 查询失败时不当作早失败信号
                }
            }
            $fatal = Get-FatalLogLine $watch.Log
            if ($null -ne $fatal) {
                $script:ReadyFailureReason = ((Msg 'Ready.FatalLog') -f $watch.Name, $fatal)
                Write-Host (' ' + (Msg 'Common.Failed')) -ForegroundColor Red
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
    Write-Host (' ' + (Msg 'Common.Timeout')) -ForegroundColor Red
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

# ---------- T05：能力探测 + 三件套守卫（§3.6） ----------
# S1–S4 在这里做：DryRun 与正式流程共用，且必须早于完成页与 pids.json 判定。
# 正常路径（Job 生效）三件套休眠；任何失败 / -NoPause / -ForceJobFallback
# 都把 $script:TrioEnabled 翻回 true，完整启用退出事件与 PID 清单。
Initialize-JobCapability
Register-ExitCleanup

Write-Host ''
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host ('   ' + (Msg 'Banner.Title')) -ForegroundColor White
Write-Host ' ============================================================' -ForegroundColor DarkCyan
if ($DryRun) { Write-Host ('   ' + (Msg 'Banner.DryRun')) -ForegroundColor Yellow }
Write-Host ''

# ---------- 0. 环境检查 ----------
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail (Msg 'Env.PythonMissing') @(
        (Msg 'Env.PythonHint1'),
        (Msg 'Env.PythonHint2')
    )
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Fail (Msg 'Env.NodeMissing') @(
        (Msg 'Env.NodeHint1'),
        (Msg 'Env.NodeHint2')
    )
}
Write-Ok (Msg 'Step.EnvOk')

# ---------- 1. 后端运行环境 ----------
if (-not (Test-Path $VenvPython)) {
    if ($DryRun) {
        Write-Step (Msg 'Step.VenvCreateDry')
    } else {
        Write-Step (Msg 'Step.VenvCreate')
        python -m venv (Join-Path $BackendDir '.venv')
        if ($LASTEXITCODE -ne 0) {
            Fail (Msg 'Fail.VenvCreate') @(
                (Msg 'Fail.VenvHint1'),
                (Msg 'Fail.VenvHint2')
            )
        }
        Write-Step (Msg 'Step.VenvInstall')
        & $VenvPython -m pip install -q --upgrade pip
        & $VenvPython -m pip install -q -r (Join-Path $BackendDir 'requirements.txt')
        if ($LASTEXITCODE -ne 0) {
            Fail (Msg 'Fail.BackendDeps') @(
                ((Msg 'Fail.DepsHint1') -f $VenvPython),
                (Msg 'Fail.DepsHint2'),
                (Msg 'Fail.DepsHint3')
            )
        }
        Write-Ok (Msg 'Step.VenvDone')
    }
} else {
    Write-Ok (Msg 'Step.VenvReady')
}

# ---------- 2. 配置文件 ----------
$EnvFile = Join-Path $BackendDir '.env'
if (-not (Test-Path $EnvFile)) {
    if ($DryRun) {
        Write-Step (Msg 'Step.EnvCreateDry')
    } else {
        Copy-Item (Join-Path $BackendDir '.env.example') $EnvFile -ErrorAction SilentlyContinue
        Write-Step (Msg 'Step.EnvCreated')
        Write-Host ('       ' + (Msg 'Env.RealModelHint')) -ForegroundColor DarkGray
    }
} else {
    Write-Ok (Msg 'Step.EnvExists')
}

# ---------- 3. 前端依赖 ----------
if (-not (Test-Path (Join-Path $FrontendDir 'node_modules'))) {
    if ($DryRun) {
        Write-Step (Msg 'Step.NpmInstallDry')
    } else {
        Write-Step (Msg 'Step.NpmInstall')
        Push-Location $FrontendDir
        npm install
        $npmExit = $LASTEXITCODE
        Pop-Location
        if ($npmExit -ne 0) {
            Fail (Msg 'Fail.Npm') @(
                (Msg 'Fail.NpmHint1'),
                (Msg 'Fail.NpmHint2')
            )
        }
        Write-Ok (Msg 'Step.NpmDone')
    }
} else {
    Write-Ok (Msg 'Step.NpmReady')
}

# ---------- 4. 示例教材 ----------
$SamplePdf = Join-Path $BackendDir 'data\sample_textbook.pdf'
if (-not (Test-Path $SamplePdf)) {
    if ($DryRun) {
        Write-Step (Msg 'Step.SampleDry')
    } else {
        Write-Step (Msg 'Step.SampleGen')
        Push-Location $BackendDir
        & $VenvPython 'scripts\make_sample_pdf.py' | Out-Null
        Pop-Location
        Write-Ok (Msg 'Step.SampleDone')
    }
} else {
    Write-Ok (Msg 'Step.SampleExists')
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
    # -ForceJobFallback 完全跳过 native 探测，标注 skipped 而不是伪报 FAIL。
    $addTypeText   = if ($ForceJobFallback) { 'skipped(-ForceJobFallback)' } elseif ($jc.AddTypeOk) { 'OK' } else { 'FAIL' }
    $createJobText = if ($ForceJobFallback) { 'skipped(-ForceJobFallback)' } elseif ($jc.CreateJobOk) { 'OK' } else { 'FAIL' }
    $killFlagText  = if ($ForceJobFallback) { 'skipped(-ForceJobFallback)' } elseif ($NoPause) { 'skipped(-NoPause)' } elseif ($jc.KillFlagApplied) { 'applied' } else { 'not-applied' }
    # S5 探针进程在 DryRun 下绝不启动（起了进程就不是「无副作用」）。
    $assignProbeText = 'deferred (verified against first real child at launch)'
    $structText = if ($script:JobStructSize -gt 0) { "$($script:JobStructSize)" } else { 'n/a (probe skipped)' }
    Write-Host (" Job capability : AddType={0}; InJob={1}; ParentJobBreakaway={2}; ParentJobUnknown={3}; CreateJob={4}; KillFlag={5}; AssignProbe={6}; StructSize={7}; KillOnClose={8}; Fallback={9}; TrioEnabled={10}" -f @(
        $addTypeText,
        $(if ($jc.InJob) { 'yes' } else { 'no' }),
        $(if ($jc.ParentJobBreakaway) { 'yes' } else { 'no' }),
        $(if ($jc.ParentJobUnknown) { 'yes' } else { 'no' }),
        $createJobText,
        $killFlagText,
        $assignProbeText,
        $structText,
        $(if ($jc.KillOnClose) { 'yes' } else { 'no' }),
        $jc.FallbackReason,
        $(if ($jc.TrioEnabled) { 'yes' } else { 'no' })
    )) -ForegroundColor Gray
    Write-Host (" Job struct     : Marshal.SizeOf(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)={0}; expected x64=144 / x86=112; assertion={1}" -f @(
        $structText,
        $script:JobSizeAssert
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
    # §7.1 的 Job race 行由 T05 回填；PidFile schema 行来自 T02。
    Write-Host ' Job race       : immediate-Assign + 2x descendant re-Assign scan (200ms); --reload KEPT' -ForegroundColor Gray
    Write-Host '                 (2-level reloader+server chain); NO CREATE_SUSPENDED; residual risk:' -ForegroundColor Gray
    Write-Host '                 a grandchild forked before the first Assign (or using CREATE_BREAKAWAY_FROM_JOB)' -ForegroundColor Gray
    Write-Host '                 may escape. Status: needs real-world verification (not verified in this session).' -ForegroundColor Gray
    Write-Host ' PidFile schema : v2 (pids/updated_at/target_url + optional processes[pid,process_name,start_time]); legacy=compat' -ForegroundColor Gray
    Write-Host ' Docs URL       : /api/docs (3 places updated; HTML left as registered legacy)' -ForegroundColor Gray
    Write-Host ''
    Write-Host (' ' + (Msg 'DryRun.ActionHeader')) -ForegroundColor Yellow
    if ($BackendState -eq 'Foreign' -or $FrontendState -eq 'Foreign') {
        $badPort = $BackendPort
        if ($BackendState -ne 'Foreign') { $badPort = $FrontendPort }
        Write-Host ('  ' + ((Msg 'DryRun.PortForeign') -f $badPort)) -ForegroundColor Gray
    } elseif ($BackendState -eq 'Ours' -or $FrontendState -eq 'Ours') {
        Write-Host ('  ' + (Msg 'DryRun.PortOurs')) -ForegroundColor Gray
    } else {
        Write-Host ('  ' + ((Msg 'DryRun.LaunchBackend') -f ((Get-DisplayPath $VenvPython) + " -m uvicorn app.main:app --reload --port $BackendPort"))) -ForegroundColor Gray
        Write-Host ('  ' + ((Msg 'DryRun.LaunchFrontend') -f "cmd.exe /c npm run dev -- --port $FrontendPort --strictPort")) -ForegroundColor Gray
    }
    Write-Host ('  ' + ((Msg 'DryRun.ReadyCheck') -f "127.0.0.1:$BackendPort", "127.0.0.1:$FrontendPort", $ReadyTimeoutSec)) -ForegroundColor Gray
    if ($NoBrowser) {
        Write-Host ('  ' + (Msg 'DryRun.NoBrowser')) -ForegroundColor Gray
    } else {
        Write-Host ('  ' + ((Msg 'DryRun.Browser') -f $TargetUrl)) -ForegroundColor Gray
    }
    Write-Host ('  ' + ((Msg 'DryRun.LogDir') -f (Get-DisplayPath $LauncherDir))) -ForegroundColor Gray
    Write-Host ''
    Write-Host (' ' + (Msg 'DryRun.Done')) -ForegroundColor Green
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
    Write-Host (' ' + (Msg 'Port.OursNoWrite')) -ForegroundColor DarkGray
    exit 0
}

# ---------- T05 S5：真实服务启动之前的 Assign 探针（§3.3） ----------
Confirm-JobAssignProbe

# ---------- 5. 启动前后端（单窗口，服务全部隐藏） ----------
Write-Host ''
Write-Step (Msg 'Step.StartServices')

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
    Add-LaunchedProcessToJob $script:BackendProc 'backend'
    Write-Ok ((Msg 'Step.BackendStarted') -f $script:BackendProc.Id)
} else {
    Write-Host (' ' + (Msg 'Step.BackendBusy')) -ForegroundColor DarkGray
}

if ($FrontendState -eq 'Free') {
    # P0-3：显式 --port + --strictPort，5173 被占时 vite 响亮失败并写进
    # frontend.err.log，而不是静默递增到 5174 让启动器打开错站。
    $script:FrontendProc = Start-Process -FilePath 'cmd.exe' `
        -ArgumentList @('/c', 'npm', 'run', 'dev', '--', '--port', "$FrontendPort", '--strictPort') `
        -WorkingDirectory $FrontendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $FrontendOut -RedirectStandardError $FrontendErr
    $script:StartedPids += $script:FrontendProc.Id
    Add-LaunchedProcessToJob $script:FrontendProc 'frontend'
    Write-Ok ((Msg 'Step.FrontendStarted') -f $script:FrontendProc.Id)
} else {
    Write-Host (' ' + (Msg 'Step.FrontendBusy')) -ForegroundColor DarkGray
}

# T05 S6：决议 6 保留 --reload，补挂扫描是唯一能覆盖 reloader+server 两层
# 竞态窗口的手段，不得省略、不得降为 1 轮。
Invoke-DescendantRescan

# 记录本脚本亲自启动的 PID。已在运行、非本脚本启动的服务绝不写进来，
# 否则按回车 / 兜底脚本会把别人的服务一起杀掉。
# T05：三件套由 $script:TrioEnabled 守卫 —— 正常路径休眠、不写 pids.json；
# 降级 / -NoPause / -ForceJobFallback 路径必须写，否则停止脚本无从定位 PID。
if ($script:TrioEnabled) {
    Write-PidFile
}

# ---------- 就绪检测（取代固定 Start-Sleep 10） ----------
if (-not (Wait-ServiceReady)) {
    Write-Host ''
    if ($script:ReadyFailureReason -eq 'timeout') {
        Write-Host (' [X] ' + (Msg 'Ready.Timeout')) -ForegroundColor Red
    } else {
        Write-Host (' [X] ' + (Msg 'Ready.EarlyExit')) -ForegroundColor Red
        Write-Host "     $script:ReadyFailureReason" -ForegroundColor Red
    }
    Write-Host (' ' + (Msg 'Fail.LogFiles')) -ForegroundColor Red
    foreach ($LogFile in $LogFiles) { Write-Host ('       ' + (Get-DisplayPath $LogFile)) -ForegroundColor DarkGray }
    Write-Host (' ' + (Msg 'Fail.ErrTailHeader')) -ForegroundColor Red
    foreach ($ErrLog in @($BackendErr, $FrontendErr)) { Write-ErrLogTail $ErrLog }
    Write-Host (' ' + (Msg 'Fail.CommonCauses')) -ForegroundColor DarkGray
    Stop-LaunchedServices
    exit 1
}

if (-not $NoBrowser) {
    Start-Process $TargetUrl | Out-Null
}

Write-Host ''
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host (' ' + (Msg 'Done.Title')) -ForegroundColor White
Write-Host ''
Write-Host ('  ' + ((Msg 'Done.FrontendUrl') -f $TargetUrl)) -ForegroundColor Gray
Write-Host ('  ' + ((Msg 'Done.ApiDocs') -f "http://127.0.0.1:$BackendPort/api/docs")) -ForegroundColor Gray
Write-Host ('  ' + ((Msg 'Done.Health') -f "http://127.0.0.1:$BackendPort/api/health")) -ForegroundColor Gray
Write-Host ('  ' + ((Msg 'Done.LogDir') -f (Get-DisplayPath $LauncherDir))) -ForegroundColor Gray
Write-Host '              backend.out.log / backend.err.log' -ForegroundColor DarkGray
Write-Host '              frontend.out.log / frontend.err.log' -ForegroundColor DarkGray
Write-Host (' ' + (Msg 'Done.InstallPwa')) -ForegroundColor Gray
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host ''

# ---------- T05：完成页的清理语义 / 用户认知防线（§3.6 / §7.4） ----------
# 正常路径（Job 完全生效、非 -NoPause）才允许出现「关窗口即停」。
# 其余任何情况都绝不出现该字样，必须明确「请按回车停止」+ 停止脚本兜底提示。
$jobReason = [string]$script:LauncherCapability.FallbackReason
$jobDegraded = [bool]$script:TrioEnabled
$capFailed = ($jobReason -ne 'none') -and (-not $jobReason.StartsWith('-NoPause'))

if (-not $jobDegraded) {
    Write-Host (' ' + (Msg 'Job.CloseStops')) -ForegroundColor Green
    Write-Host (' ' + (Msg 'Job.RaceNotice')) -ForegroundColor DarkGray
} else {
    if ($capFailed) {
        $fallbackLine = (Msg 'Job.Fallback').Replace('<R>', $jobReason)
        Write-Host (' ' + $fallbackLine) -ForegroundColor Yellow
    }
    Write-Host (' ' + (Msg 'Stop.CloseHint')) -ForegroundColor DarkGray
}
Write-Host ''

if ($NoPause) {
    Write-Host (' ' + (Msg 'Job.NoPause')) -ForegroundColor Yellow
    Write-Host (' ' + (Msg 'Stop.NoPause1')) -ForegroundColor Yellow
    Write-Host (' ' + (Msg 'Stop.NoPause2')) -ForegroundColor Yellow
    Write-Host (' ' + (Msg 'Stop.NoPause3')) -ForegroundColor Yellow
    exit 0
}

Write-Host (' ' + (Msg 'Stop.PressEnter')) -ForegroundColor White
Write-Host ''
Read-Host (' ' + (Msg 'Stop.ReadHost')) | Out-Null
Stop-LaunchedServices
Write-Ok (Msg 'Stop.Done')
