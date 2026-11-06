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
    本文件必须以 UTF-8 with BOM 保存（同 start-web.ps1）。
#>
param()

$ErrorActionPreference = 'Stop'

try {
    chcp 65001 > $null
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 旧版控制台忽略即可
}

$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root 'backend\data\launcher\pids.json'

Write-Host ''
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host '   停止后台服务（兜底清理）' -ForegroundColor White
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host ''

if (-not (Test-Path $PidFile)) {
    Write-Host ' 没有找到 pids.json —— 没有需要清理的后台服务。' -ForegroundColor Green
    Write-Host " 预期位置：$PidFile" -ForegroundColor DarkGray
    exit 0
}

try {
    $Data = Get-Content -Path $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Host ' [X] pids.json 解析失败，已删除该文件以免下次误用。' -ForegroundColor Red
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 1
}

# $PID 是 PowerShell 的只读自动变量，循环变量必须叫别的名字。
$ProcIds = @($Data.pids)
if ($ProcIds.Count -eq 0) {
    Write-Host ' pids.json 里没有本脚本启动的 PID（服务可能是外部启动的），不清理。' -ForegroundColor Green
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

foreach ($ProcId in $ProcIds) {
    if (-not $ProcId -or $ProcId -le 0) { continue }
    Write-Host " 停止进程树 PID $ProcId ..." -ForegroundColor Gray
    # /T 连同子进程一起杀：--reload 的 uvicorn 子进程、npm run dev 的 node 子进程
    # 都会在只杀父进程时留下来继续占端口。
    # 进程已退出时 taskkill 会写 stderr；$ErrorActionPreference='Stop' 下这会被当成
    # 终止性错误，导致后面的 PID 不再处理、pids.json 也删不掉。
    # 用 cmd.exe 在子进程内 >nul 2>&1 吞掉输出，再逐个 try/catch 保证循环走完。
    try { & cmd.exe /c "taskkill /PID $ProcId /T /F >nul 2>&1" } catch { }
}

Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
Write-Host ''
Write-Host ' 已清理。若浏览器仍显示页面，刷新即可（服务已停止）。' -ForegroundColor Green
Write-Host ''
exit 0
