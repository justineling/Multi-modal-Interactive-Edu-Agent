@echo off
rem ============================================================================
rem  ASCII-only stop launcher. DO NOT put Chinese characters in this file.
rem
rem  Use this only if you closed the start window with the X button instead of
rem  pressing Enter. The normal path (press Enter in the start window) already
rem  kills the whole process tree and removes the PID file.
rem
rem  All logic and Chinese messages live in scripts\stop-web.ps1 (UTF-8 BOM).
rem ============================================================================
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-web.ps1"
echo.
pause
endlocal
