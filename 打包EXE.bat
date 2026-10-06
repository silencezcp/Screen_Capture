@echo off
chcp 936 >nul
rem 一键打包成 exe：双击本文件即可
rem 会先用 setup_env.py 检查依赖（缺什么自动用国内镜像下载），再用 PyInstaller 打包
setlocal
cd /d "%~dp0"

set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE call :try "py -3"
if not defined PYEXE call :try "python"
if not defined PYEXE call :try "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

if not defined PYEXE (
    echo 没有找到可用的 Python，请先安装 Python 3.9 及以上版本：https://www.python.org/downloads/
    pause
    exit /b 1
)

echo 使用的 Python：%PYEXE%
echo.
echo [1/2] 检查依赖（缺的会用 清华/阿里云 镜像自动下载）...
%PYEXE% "%~dp0setup_env.py" --no-venv
if errorlevel 1 (
    echo.
    echo 依赖准备失败，请按上面的提示手动安装后重试。
    pause
    exit /b 1
)

echo.
echo [2/2] 开始打包（首次打包需要几分钟）...
%PYEXE% "%~dp0build_exe.py" %*
if errorlevel 1 goto :error

echo.
echo 打包完成，产物在 dist 目录里。
pause
exit /b 0

:error
echo.
echo 打包失败，请查看上面的输出。
pause
exit /b 1

:try
if defined PYEXE exit /b 0
%~1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
exit /b 0
