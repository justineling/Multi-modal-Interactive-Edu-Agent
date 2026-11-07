<#
.SYNOPSIS
    停止「1. 启动网页端(Windows).bat」启动的后台服务（Windows 兜底脚本）

.DESCRIPTION
    正常流程是「在启动窗口里按回车」，那一条路径会直接杀树并清理 PID 文件。
    本脚本用于用户**直接点 X 关掉启动窗口**的情况：读 pids.json，
    对其中每个 PID 执行 taskkill /T /F 杀掉整棵进程树，然后删除 PID 文件。

    只杀「启动脚本亲自启动」的 PID。端口本来就被别的服务占用时，
    启动脚本不会把那些 PID 写进 pids.json，所以这里也不会误杀别人的服务。

.NOTES
    本文件必须以 UTF-8 with BOM + CRLF 保存（同 start-web.ps1）。
    T01：chcp 条件化、中文提示改走 Msg() 中英资源表、行尾统一 CRLF。
    三处注册表只读与六档分支在 T03 接入；P1-3 四态校验在 T02 接入。
#>
param()

$ErrorActionPreference = 'Stop'

# ---------- T01 基础设施：中英资源表 / Msg() / 控制台能力骨架 ----------

# 所有用户可见文案集中在此；新增文案必须 zh / en 成对，en 分支必须纯 ASCII。
$Messages = @{
    zh = @{
        'StopScript.Title'       = '停止后台服务（兜底清理）'
        'StopScript.NoPid'       = '没有找到 pids.json —— 没有需要清理的后台服务。'
        'StopScript.PidPath'     = '预期位置：{0}'
        'StopScript.ParseFailed' = '[X] pids.json 解析失败，已删除该文件以免下次误用。'
        'StopScript.EmptyPids'   = 'pids.json 里没有本脚本启动的 PID（服务可能是外部启动的），不清理。'
        'StopScript.KillTree'    = '停止进程树 PID {0} ...'
        'StopScript.Cleaned'     = '已清理。若浏览器仍显示页面，刷新即可（服务已停止）。'
        'StopScript.Stale'       = 'pids.json 已过期（超过 24 小时），为避免误杀已删除，未停止任何进程。请用 netstat -ano | findstr :8000,5173 定位。'
        'StopScript.Mismatch'    = 'pids.json 中的进程身份不匹配（PID 可能已被复用），已删除，未停止任何进程。'
        'StopScript.Legacy'      = 'pids.json 为旧格式（无进程身份字段），按兼容模式处理。'
    }
    en = @{
        'StopScript.Title'       = 'Stop background services (fallback cleanup)'
        'StopScript.NoPid'       = 'No pids.json found - nothing to clean up.'
        'StopScript.PidPath'     = 'Expected location: {0}'
        'StopScript.ParseFailed' = '[X] Failed to parse pids.json; file deleted to avoid reuse.'
        'StopScript.EmptyPids'   = 'pids.json has no PIDs started by this script (services may be external); nothing cleaned.'
        'StopScript.KillTree'    = 'Stopping process tree PID {0} ...'
        'StopScript.Cleaned'     = 'Cleaned up. Refresh the browser if the page is still shown (services stopped).'
        'StopScript.Stale'       = 'pids.json is stale (>24h); deleted without killing any process. Locate services with: netstat -ano | findstr :8000,5173'
        'StopScript.Mismatch'    = 'Process identity in pids.json does not match (PID may have been reused); deleted without killing any process.'
        'StopScript.Legacy'      = 'pids.json uses the legacy format (no identity fields); handling in compatibility mode.'
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
    # 三处注册表只读与六档分支在 T03 实现。
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

$script:ConsoleCapability = Get-ConsoleTextCapability
$cap = $script:ConsoleCapability

try {
    if ($cap.DoChcp) {
        chcp 65001 > $null
    }
    [Console]::OutputEncoding = $cap.OutputEncoding
} catch {
    # 旧版控制台忽略即可
}

$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root 'backend\data\launcher\pids.json'

Write-Host ''
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host ('   ' + (Msg 'StopScript.Title')) -ForegroundColor White
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host ''

if (-not (Test-Path $PidFile)) {
    Write-Host (' ' + (Msg 'StopScript.NoPid')) -ForegroundColor Green
    Write-Host (' ' + ((Msg 'StopScript.PidPath') -f $PidFile)) -ForegroundColor DarkGray
    exit 0
}

try {
    $Data = Get-Content -Path $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Host (' ' + (Msg 'StopScript.ParseFailed')) -ForegroundColor Red
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 1
}

# ---------- P1-3（C4）：四态判定（§2.5）----------
# A 新格式且身份全匹配 → 才 taskkill；B 陈旧 >24h → 只删不杀；
# C 身份不匹配 / 查不到进程 → 只删不杀（宁可漏停，不可误杀）；
# D 旧格式（processes 缺失或为空）→ 按旧行为处理、不报错。
# $PID 是 PowerShell 的只读自动变量，循环变量必须叫别的名字。
$ProcIds = @($Data.pids)
$Entries = @()
if ($null -ne $Data.processes) { $Entries = @($Data.processes) }

function Remove-PidFileAndExit([string]$MessageKey, [string]$Color) {
    Write-Host (' ' + (Msg $MessageKey)) -ForegroundColor $Color
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

if ($ProcIds.Count -eq 0) {
    Remove-PidFileAndExit 'StopScript.EmptyPids' 'Green'
}

# ---- 状态 B：陈旧（updated_at 距今 > 24 小时）----
$Stale = $false
if ($Data.updated_at) {
    try {
        $UpdatedAt = [datetime]::Parse(
            [string]$Data.updated_at,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind)
        if (((Get-Date).ToUniversalTime() - $UpdatedAt.ToUniversalTime()).TotalHours -gt 24) {
            $Stale = $true
        }
    } catch {
        # 时间戳解析失败不当作陈旧：交给后面的身份校验决定，避免误判后放弃停止
    }
}
if ($Stale) { Remove-PidFileAndExit 'StopScript.Stale' 'Yellow' }

# ---- 状态 D：旧格式（processes 缺失 / 为空）→ 旧行为，不报错 ----
if ($Entries.Count -eq 0) {
    Write-Host (' ' + (Msg 'StopScript.Legacy')) -ForegroundColor Yellow
    foreach ($ProcId in $ProcIds) {
        if (-not $ProcId -or $ProcId -le 0) { continue }
        Write-Host (' ' + ((Msg 'StopScript.KillTree') -f $ProcId)) -ForegroundColor Gray
        # /T 连同子进程一起杀：--reload 的 uvicorn 子进程、npm run dev 的 node 子进程
        # 都会在只杀父进程时留下来继续占端口。
        # 进程已退出时 taskkill 会写 stderr；$ErrorActionPreference='Stop' 下这会被当成
        # 终止性错误，导致后面的 PID 不再处理、pids.json 也删不掉。
        # 用 cmd.exe 在子进程内 >nul 2>&1 吞掉输出，再逐个 try/catch 保证循环走完。
        try { & cmd.exe /c "taskkill /PID $ProcId /T /F >nul 2>&1" } catch { }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host ''
    Write-Host (' ' + (Msg 'StopScript.Cleaned')) -ForegroundColor Green
    Write-Host ''
    exit 0
}

# ---- 状态 A / C：逐个校验 ProcessName + StartTime ----
# 任一 PID 查不到、进程名不符、启动时间不符 → 整体落入 C，一个都不杀。
$Verified = @()
$Mismatch = $false
foreach ($Entry in $Entries) {
    $EntryId = [int]$Entry.pid
    if (-not $EntryId -or $EntryId -le 0) { $Mismatch = $true; break }
    try {
        $Proc = Get-Process -Id $EntryId -ErrorAction Stop
        $NameOk = ([string]$Proc.ProcessName -eq [string]$Entry.process_name)
        $StartOk = ($Proc.StartTime.ToString('o') -eq [string]$Entry.start_time)
        if (-not ($NameOk -and $StartOk)) { $Mismatch = $true; break }
        $Verified += $EntryId
    } catch {
        $Mismatch = $true
        break
    }
}
if ($Mismatch) { Remove-PidFileAndExit 'StopScript.Mismatch' 'Yellow' }

# ---- 状态 A：全部匹配，才杀 ----
foreach ($ProcId in $Verified) {
    Write-Host (' ' + ((Msg 'StopScript.KillTree') -f $ProcId)) -ForegroundColor Gray
    try { & cmd.exe /c "taskkill /PID $ProcId /T /F >nul 2>&1" } catch { }
}

Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
Write-Host ''
Write-Host (' ' + (Msg 'StopScript.Cleaned')) -ForegroundColor Green
Write-Host ''
exit 0
