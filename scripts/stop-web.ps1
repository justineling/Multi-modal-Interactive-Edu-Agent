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
    }
    en = @{
        'StopScript.Title'       = 'Stop background services (fallback cleanup)'
        'StopScript.NoPid'       = 'No pids.json found - nothing to clean up.'
        'StopScript.PidPath'     = 'Expected location: {0}'
        'StopScript.ParseFailed' = '[X] Failed to parse pids.json; file deleted to avoid reuse.'
        'StopScript.EmptyPids'   = 'pids.json has no PIDs started by this script (services may be external); nothing cleaned.'
        'StopScript.KillTree'    = 'Stopping process tree PID {0} ...'
        'StopScript.Cleaned'     = 'Cleaned up. Refresh the browser if the page is still shown (services stopped).'
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

# $PID 是 PowerShell 的只读自动变量，循环变量必须叫别的名字。
$ProcIds = @($Data.pids)
if ($ProcIds.Count -eq 0) {
    Write-Host (' ' + (Msg 'StopScript.EmptyPids')) -ForegroundColor Green
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

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
