@echo off
chcp 936 >nul
rem 一键打包成 exe：双击本文件即可
rem 会依次尝试：项目内 .venv -> py -3 -> python -> DSH 运行库自带 Python
setlocal
cd /d "%~dp0"

if exist "%~dp0packages\PyInstaller" set "PYTHONPATH=%~dp0packages"

set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE call :try "py -3"
if not defined PYEXE call :try "python"
if not defined PYEXE call :try "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

if not defined PYEXE (
    echo 没有找到可用的 Python，请安装 Python 3.9 及以上版本：https://www.python.org/downloads/
    pause
    exit /b 1
)

%PYEXE% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo 正在安装打包工具 PyInstaller ...
    %PYEXE% -m pip install pyinstaller || goto :error
)

echo 正在打包（目录版，第一次打包需要几分钟）...
%PYEXE% build_exe.py %*
if errorlevel 1 goto :error

echo.
echo 打包完成，产物在 dist 目录：
dir /b dist
pause
exit /b 0

:error
echo.
echo 打包失败，请把上面的错误信息一并反馈。
pause
exit /b 1

:try
if defined PYEXE exit /b 0
%~1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
exit /b 0