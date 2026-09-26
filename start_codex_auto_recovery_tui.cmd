@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_codex_auto_recovery_tui.ps1" %*
set EXIT_CODE=%ERRORLEVEL%
endlocal & exit /b %EXIT_CODE%
