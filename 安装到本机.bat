@echo off
chcp 936 >nul
rem ============================================================
rem  把打包好的程序安装到本机（复制到普通目录 + 建桌面快捷方式）
rem
rem  为什么要这一步：如果程序放在带「低完整性标签」的目录里（例如某些
rem  沙箱工作区），exe 启动后会被降级成低权限，WGC 抓窗口会被 Windows
rem  拒绝（0x80070005 没有捕获权限）。复制到 %LOCALAPPDATA% 后新文件
rem  继承普通权限标签，WGC 就正常了。
rem ============================================================
setlocal
cd /d "%~dp0"

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

if exist "%~dp0.packages\PyInstaller" set "PYTHONPATH=%~dp0.packages"

echo 正在安装到 %LOCALAPPDATA%\ScreenCaptureTool ...
%PYEXE% build_exe.py --deploy-only
if errorlevel 1 goto :error

set "TARGET=%LOCALAPPDATA%\ScreenCaptureTool\app\应用窗口定时截图工具.exe"
if not exist "%TARGET%" set "TARGET=%LOCALAPPDATA%\ScreenCaptureTool\app\app.exe"
if exist "%TARGET%" (
    powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\应用窗口定时截图工具.lnk'); $s.TargetPath='%TARGET%'; $s.WorkingDirectory=Split-Path '%TARGET%'; $s.Save()" >nul 2>nul
    echo 已创建桌面快捷方式。
)

echo.
echo 安装完成：%LOCALAPPDATA%\ScreenCaptureTool
echo 直接运行那里的 exe（或桌面快捷方式）即可，WGC 抓窗口功能可正常使用。
pause
exit /b 0

:error
echo.
echo 安装失败，请先把程序打包好（双击 打包EXE.bat）。
pause
exit /b 1

:try
if defined PYEXE exit /b 0
%~1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
exit /b 0
