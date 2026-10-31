@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -File "%~dp0launchers\start-qwen.ps1"
echo.
echo The server window has stopped. Press any key to close.
pause >nul
