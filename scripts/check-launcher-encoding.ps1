<#
.SYNOPSIS
    Windows 一键启动器 · 编码 / 语法提交前自检（只读、无副作用）

.DESCRIPTION
    逐条校验启动器的编码契约与 PowerShell 语法：
      1. BAT-ASCII  两个 .bat 全部字节 < 0x80（100% 纯 ASCII，含注释，且不含 BOM）；
      2. PS1-BOM    两个 .ps1（及本脚本）前 3 字节必须是 EF BB BF（UTF-8 with BOM）；
      3. PS1-CRLF   两个 .ps1（及本脚本）行尾统一 CRLF，不允许孤立 LF / CR；
      4. PS1-PARSE  三个 .ps1 经 [Parser]::ParseFile 解析，ParseErrors 必须为 0；
      5. MSG-PAIR   两个 .ps1 的 $Messages 资源表 zh / en 键必须成对，且 en 值纯 ASCII。
    任何一条不满足都会打印「规则 / 文件 / 实测值」，进程退出码 1；全部通过退出码 0。

.NOTES
    本文件必须以 UTF-8 with BOM + CRLF 保存。
    可复用：新增启动器文案若只写了 zh，或 en 值含非 ASCII 字节，本脚本会在提交前报错。
#>
param()

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$BatFiles = @(
    (Join-Path $Root '1. 启动网页端(Windows).bat'),
    (Join-Path $Root '3. 停止网页端(Windows).bat')
)
$LauncherPs1 = @(
    (Join-Path $PSScriptRoot 'start-web.ps1'),
    (Join-Path $PSScriptRoot 'stop-web.ps1')
)
$AllPs1 = @($LauncherPs1) + @($PSCommandPath)

$script:Failures = 0

function Write-Pass([string]$Rule, [string]$Detail) {
    Write-Host (' [PASS] {0} :: {1}' -f $Rule, $Detail) -ForegroundColor Green
}

function Write-Fail([string]$Rule, [string]$Path, [string]$Detail) {
    $script:Failures++
    Write-Host (' [FAIL] {0} :: {1} :: {2}' -f $Rule, (Split-Path -Leaf $Path), $Detail) -ForegroundColor Red
}

function Read-FileBytes([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return [System.IO.File]::ReadAllBytes($Path)
}

Write-Host ''
Write-Host ' ============================================================' -ForegroundColor DarkCyan
Write-Host '   Launcher encoding / syntax self-check (T01)' -ForegroundColor White
Write-Host ' ============================================================' -ForegroundColor DarkCyan

# ---------- 1. 两个 .bat 100% 纯 ASCII ----------
foreach ($p in $BatFiles) {
    $bytes = Read-FileBytes $p
    if ($null -eq $bytes) { Write-Fail 'BAT-ASCII' $p 'file not found'; continue }
    $badOffsets = @()
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        if ($bytes[$i] -ge 0x80) { $badOffsets += $i }
    }
    if ($badOffsets.Count -gt 0) {
        Write-Fail 'BAT-ASCII' $p ('{0} byte(s) >= 0x80; first at offset {1} (0x{2:X2}); .bat must be 100% ASCII' -f $badOffsets.Count, $badOffsets[0], $bytes[$badOffsets[0]])
    } else {
        Write-Pass 'BAT-ASCII' ('{0}: {1} bytes, all < 0x80' -f (Split-Path -Leaf $p), $bytes.Length)
    }
}

# ---------- 2. 三个 .ps1 UTF-8 with BOM（前 3 字节 EF BB BF） ----------
foreach ($p in $AllPs1) {
    $bytes = Read-FileBytes $p
    if ($null -eq $bytes) { Write-Fail 'PS1-BOM' $p 'file not found'; continue }
    if ($bytes.Length -lt 3) { Write-Fail 'PS1-BOM' $p 'file shorter than 3 bytes'; continue }
    $head = ('{0:X2} {1:X2} {2:X2}' -f $bytes[0], $bytes[1], $bytes[2])
    if ($bytes[0] -ne 0xEF -or $bytes[1] -ne 0xBB -or $bytes[2] -ne 0xBF) {
        Write-Fail 'PS1-BOM' $p ('first 3 bytes = {0}; expected EF BB BF' -f $head)
    } else {
        Write-Pass 'PS1-BOM' ('{0}: {1}' -f (Split-Path -Leaf $p), $head)
    }
}

# ---------- 3. 三个 .ps1 行尾统一 CRLF ----------
foreach ($p in $AllPs1) {
    $bytes = Read-FileBytes $p
    if ($null -eq $bytes) { Write-Fail 'PS1-CRLF' $p 'file not found'; continue }
    $crlfCount = 0
    $loneLfCount = 0
    $loneCrCount = 0
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        if ($bytes[$i] -eq 0x0A) {
            if ($i -gt 0 -and $bytes[$i - 1] -eq 0x0D) { $crlfCount++ } else { $loneLfCount++ }
        } elseif ($bytes[$i] -eq 0x0D) {
            if ($i + 1 -ge $bytes.Length -or $bytes[$i + 1] -ne 0x0A) { $loneCrCount++ }
        }
    }
    if ($loneLfCount -gt 0 -or $loneCrCount -gt 0 -or $crlfCount -eq 0) {
        Write-Fail 'PS1-CRLF' $p ('CRLF={0}, lone-LF={1}, lone-CR={2}; all line endings must be CRLF' -f $crlfCount, $loneLfCount, $loneCrCount)
    } else {
        Write-Pass 'PS1-CRLF' ('{0}: {1} CRLF, 0 lone LF/CR' -f (Split-Path -Leaf $p), $crlfCount)
    }
}

# ---------- 4. PowerShell ParseErrors = 0 ----------
foreach ($p in $AllPs1) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$tokens, [ref]$parseErrors) | Out-Null
    if ($null -eq $parseErrors) {
        Write-Fail 'PS1-PARSE' $p 'ParseFile returned a null error collection'
    } elseif ($parseErrors.Count -ne 0) {
        $detail = ($parseErrors | ForEach-Object { $_.Message }) -join ' | '
        Write-Fail 'PS1-PARSE' $p ('ParseErrors={0}: {1}' -f $parseErrors.Count, $detail)
    } else {
        Write-Pass 'PS1-PARSE' ('{0}: ParseErrors=0' -f (Split-Path -Leaf $p))
    }
}

# ---------- 5. $Messages 资源表 zh / en 成对 + en 纯 ASCII ----------
function Get-MessageKeySet([string]$Path) {
    $keys = @{ zh = @(); en = @() }
    $nonAsciiEn = @()
    $lang = ''
    foreach ($line in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        if ($line -match '^\s*(zh|en)\s*=\s*@\{\s*$') { $lang = $Matches[1]; continue }
        if ($line -match '^\s*\}\s*$') { $lang = ''; continue }
        if ($lang -eq '') { continue }
        if ($line -match "^\s*'([^']+)'\s*=\s*'(.*)'\s*$") {
            $key = $Matches[1]
            $value = $Matches[2]
            $keys[$lang] += $key
            if ($lang -eq 'en' -and $value -match '[^\x00-\x7F]') { $nonAsciiEn += $key }
        }
    }
    return [pscustomobject]@{ Keys = $keys; NonAsciiEn = @($nonAsciiEn) }
}

foreach ($p in $LauncherPs1) {
    $set = Get-MessageKeySet $p
    $zhOnly = @($set.Keys.zh | Where-Object { $set.Keys.en -notcontains $_ })
    $enOnly = @($set.Keys.en | Where-Object { $set.Keys.zh -notcontains $_ })
    if ($zhOnly.Count -gt 0 -or $enOnly.Count -gt 0) {
        Write-Fail 'MSG-PAIR' $p ('zh-only=[{0}]; en-only=[{1}]; every message key needs both zh and en' -f ($zhOnly -join ','), ($enOnly -join ','))
    } elseif ($set.NonAsciiEn.Count -gt 0) {
        Write-Fail 'MSG-PAIR' $p ('en value(s) contain non-ASCII: [{0}]' -f ($set.NonAsciiEn -join ','))
    } else {
        Write-Pass 'MSG-PAIR' ('{0}: {1} keys, zh/en paired, en all ASCII' -f (Split-Path -Leaf $p), $set.Keys.zh.Count)
    }
}

Write-Host ''
if ($script:Failures -gt 0) {
    Write-Host (' Self-check FAILED: {0} rule(s) not satisfied.' -f $script:Failures) -ForegroundColor Red
    exit 1
}
Write-Host ' Self-check PASSED: all encoding / syntax rules satisfied.' -ForegroundColor Green
exit 0
