@echo off
rem ============================================================================
rem  ASCII-only launcher. DO NOT put Chinese characters in this file.
rem
rem  Why: a .bat containing non-ASCII bytes only parses correctly under the
rem  codepage it was saved in (936/GBK). On an English Windows (437/1252) the
rem  lines get truncated and commands go haywire. Keeping this file pure ASCII
rem  makes it parse identically on every Windows regardless of locale.
rem
rem  All logic and all Chinese messages live in scripts\start-web.ps1,
rem  which is saved as UTF-8 with BOM so PowerShell reads it correctly too.
rem ============================================================================
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-web.ps1"
if errorlevel 1 (
    echo.
    echo Launcher exited with an error. See messages above.
    pause
)
endlocal
