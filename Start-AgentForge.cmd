@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -File "%~dp0launchers\start-all.ps1" -EmbeddingOnly
echo.
echo Press any key to close.
pause >nul
