@echo off
chcp 936 >nul
rem ============================================================
rem  把打包好的程序安装到本机：
rem    1) 复制到 %LOCALAPPDATA%\ScreenCaptureTool（普通目录，权限正常，WGC 可用）
rem    2) 在桌面创建快捷方式
rem
rem  为什么要复制：如果程序放在带「低完整性标签」的目录里（沙箱 / 受限工作区），
rem  exe 启动后会被降级成低权限，WGC 抓窗口会被 Windows 拒绝（0x80070005）。
rem ============================================================
setlocal
cd /d "%~dp0"

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

echo 正在安装到 %LOCALAPPDATA%\ScreenCaptureTool ...
%PYEXE% build_exe.py --deploy-only
if errorlevel 1 goto :error

echo.
echo 安装完成。桌面已创建「应用窗口定时截图工具」快捷方式，
echo 也可以直接运行：%LOCALAPPDATA%\ScreenCaptureTool\app\应用窗口定时截图工具.exe
pause
exit /b 0

:error
echo.
echo 安装失败：请先双击「打包EXE.bat」把程序打包好，再运行本脚本。
pause
exit /b 1

:try
if defined PYEXE exit /b 0
%~1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
exit /b 0