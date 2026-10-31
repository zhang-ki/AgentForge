@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -File "%~dp0launchers\start-all.ps1"
set "AF_EXIT=%ERRORLEVEL%"
echo.
if not "%AF_EXIT%"=="0" echo AgentForge startup stopped with code %AF_EXIT%.
echo Press any key to close this window.
pause >nul
exit /b %AF_EXIT%
