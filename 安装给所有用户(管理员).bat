@echo off
chcp 936 >nul
rem ============================================================
rem  把本程序安装给「本机所有用户」（服务器 / 多人同时使用）
rem
rem  做四件事：
rem    1) 复制到 C:\ProgramData\ScreenCaptureTool（普通目录，权限正常，WGC 可用）
rem    2) 目录设为「所有用户只读+执行、管理员可写」——只读是故意的：
rem       程序目录不可写时，程序会把日志和默认截图目录放到每个用户自己的
rem       %LOCALAPPDATA%\ScreenCaptureTool，多人同时跑互不干扰
rem    3) 放置 multi_user.txt 标记，程序据此确认"多人共用"模式
rem    4) 在「公用桌面」建快捷方式，所有登录用户都能看到
rem
rem  需要管理员权限（写 ProgramData + 改 ACL + 写公用桌面）。
rem  远程桌面（RDS）场景：每个用户在自己的会话里运行，会话之间互相隔离。
rem ============================================================
setlocal
cd /d "%~dp0"

net session >nul 2>nul
if errorlevel 1 (
    echo 本脚本需要管理员权限：请右键本文件 -^> 以管理员身份运行。
    pause
    exit /b 1
)

if exist "%~dp0packages\PyInstaller" set "PYTHONPATH=%~dp0packages"

set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE call :try "py -3"
if not defined PYEXE call :try "python"
if not defined PYEXE call :try "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PYEXE (
    echo 没有找到可用的 Python，请安装 Python 3.9 及以上版本。
    pause
    exit /b 1
)

echo 正在安装给本机所有用户 ...
echo   程序目录：%PROGRAMDATA%\ScreenCaptureTool
echo   每个用户的截图/日志：%%LOCALAPPDATA%%\ScreenCaptureTool
echo.
%PYEXE% build_exe.py --deploy-only --all-users
if errorlevel 1 goto :error

echo.
echo 安装完成：所有用户都能在桌面（公用桌面）看到「应用窗口定时截图工具」。
echo 也可以直接运行：%PROGRAMDATA%\ScreenCaptureTool\app\应用窗口定时截图工具.exe
pause
exit /b 0

:error
echo.
echo 安装失败：请先双击「打包EXE.bat」把程序打包好，再以管理员身份运行本脚本。
echo 如果提示文件被占用，请先让所有用户退出正在运行的程序。
pause
exit /b 1

:try
if defined PYEXE exit /b 0
%~1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
exit /b 0
