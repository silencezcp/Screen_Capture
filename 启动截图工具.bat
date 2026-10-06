@echo off
chcp 936 >nul
rem 双击启动「应用窗口定时截图工具」（PyQt6 界面）
rem 会依次尝试：项目内 .venv -> py -3 -> python -> DSH 运行库自带 Python
setlocal
cd /d "%~dp0"

rem 依赖装在项目内的 packages 目录时，让 Python 能找到 numpy / wgc_python
if exist "%~dp0packages\PyQt5" set "PYTHONPATH=%~dp0packages"
if exist "%~dp0packages\PyQt6" set "PYTHONPATH=%~dp0packages"

set "PYEXE="
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYEXE=%~dp0.venv\Scripts\pythonw.exe"
if not defined PYEXE call :try "py -3"
if not defined PYEXE call :try "python"
if not defined PYEXE call :try "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

if not defined PYEXE (
    echo 没有找到可用的 Python，请安装 Python 3.9 及以上版本：https://www.python.org/downloads/
    pause
    exit /b 1
)

start "" %PYEXE% "%~dp0run.py" %*
exit /b 0

:try
if defined PYEXE exit /b 0
%~1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
exit /b 0