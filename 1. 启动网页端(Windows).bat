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
rem
rem  %* forwards all arguments (-TargetUrl, -NoPause, -NoBrowser, -DryRun).
rem ============================================================================
setlocal
rem  Automation: suppress the error pause when -NoPause / -DryRun is passed.
rem  Guard on %~1 because "for %%A in ()" is a syntax error when no argument is given.
if not "%~1"=="" for %%A in (%*) do if /I "%%~A"=="-NoPause" set "EDU_NOPAUSE=1"
if not "%~1"=="" for %%A in (%*) do if /I "%%~A"=="-DryRun" set "EDU_NOPAUSE=1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-web.ps1" %*
if errorlevel 1 (
    echo.
    echo Launcher exited with an error. See messages above.
    if not defined EDU_NOPAUSE pause
)
endlocal
