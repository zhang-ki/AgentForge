@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -File "%~dp0launchers\start-agentforge.ps1"
echo.
echo Press any key to close.
pause >nul
