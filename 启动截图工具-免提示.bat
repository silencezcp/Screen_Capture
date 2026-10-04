@echo off
chcp 936 >nul
rem ============================================================
rem  启动「应用窗口定时截图工具」（优先用安装到本机的版本）
rem
rem  优先：%LOCALAPPDATA%\ScreenCaptureTool\app  （普通目录，权限正常，WGC 可用）
rem  其次：dist 里刚打包的那份（若在受限工作区里，WGC 可能被系统拒绝）
rem  另外会先清一遍「来自网络」标记，减少 SmartScreen 拦截。
rem ============================================================
setlocal
cd /d "%~dp0"

set "EXE=%LOCALAPPDATA%\ScreenCaptureTool\app\应用窗口定时截图工具.exe"
if not exist "%EXE%" set "EXE=%~dp0dist\应用窗口定时截图工具\应用窗口定时截图工具.exe"

if not exist "%EXE%" (
    echo 没有找到可运行的程序。
    echo 请先双击「打包EXE.bat」打包，再双击「安装到本机.bat」安装；
    echo 或者直接双击「启动截图工具.bat」从源码运行。
    pause
    exit /b 1
)

powershell -NoProfile -Command "Get-ChildItem -LiteralPath (Split-Path '%EXE%') -Recurse -File | Unblock-File -ErrorAction SilentlyContinue" >nul 2>nul

echo 正在启动：应用窗口定时截图工具
echo   %EXE%
start "" "%EXE%" %*
exit /b 0