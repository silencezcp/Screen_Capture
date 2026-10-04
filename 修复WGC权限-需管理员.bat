@echo off
chcp 936 >nul
rem ============================================================
rem  修复 WGC 权限（需要管理员）
rem
rem  症状：程序提示「没有捕获权限」（HRESULT 0x80070005），截被遮挡的窗口时
rem        会把压在上面的窗口一起截进去。
rem  原因：程序所在目录带了「低完整性标签」（沙箱 / 受限工作区 / 某些同步盘），
rem        从这里启动的 exe 会被降级为低权限进程，而 Windows 只允许 Medium
rem        及以上权限的进程使用 WGC 抓窗口。
rem  本脚本把当前目录的完整性标签改回 Medium（提升标签需要管理员权限）。
rem ============================================================
setlocal
cd /d "%~dp0"

net session >nul 2>nul
if errorlevel 1 (
    echo 请右键本文件 -^> 以管理员身份运行。
    pause
    exit /b 1
)

echo 正在把下面目录的完整性标签改回 Medium：
echo   %~dp0
echo.
icacls "%~dp0." /setintegritylevel "(OI)(CI)Medium" /T /C
if errorlevel 1 goto :error

echo.
echo 完成。现在直接运行本目录下的程序即可，WGC 应该可用了。
echo 用程序自带的自检确认：程序.exe --selftest  （「运行权限」应显示 Medium）
pause
exit /b 0

:error
echo.
echo 修改失败（可能仍有文件被占用）。可以改用更简单的办法：
echo   把整个程序文件夹复制到桌面 / 文档 / D:\Tools 这类普通目录再运行。
pause
exit /b 1