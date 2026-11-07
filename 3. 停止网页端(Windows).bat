@echo off
rem ============================================================================
rem  ASCII-only stop launcher. DO NOT put Chinese characters in this file.
rem
rem  T05: this file is RETAINED on purpose. It is the manual stop entry point
rem  for the degraded path (start window prints "Auto-cleanup NOT enabled") and
rem  for -NoPause mode. On the normal path the start window prints the correct
rem  stop instruction; follow that, or use this script as the fallback.
rem
rem  All logic and Chinese messages live in scripts\stop-web.ps1 (UTF-8 BOM).
rem ============================================================================
setlocal
rem  Automation: suppress the trailing pause when -NoPause / -DryRun is passed.
rem  Scan only -- scripts\stop-web.ps1 takes no parameters, so %* is NOT forwarded.
rem  Guard on %~1 because "for %%A in ()" is a syntax error when no argument is given.
if not "%~1"=="" for %%A in (%*) do if /I "%%~A"=="-NoPause" set "EDU_NOPAUSE=1"
if not "%~1"=="" for %%A in (%*) do if /I "%%~A"=="-DryRun" set "EDU_NOPAUSE=1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-web.ps1"
echo.
if not defined EDU_NOPAUSE pause
endlocal
