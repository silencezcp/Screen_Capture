@echo off
chcp 936 >nul
rem ============================================================
rem  修复「所有用户」安装后的文件权限（需要管理员）
rem
rem  症状：双击 C:\ProgramData\ScreenCaptureTool\app\应用窗口定时截图工具.exe
rem        报「Windows 无法访问指定设备、路径或文件。你可能没有适当的权限访问该项目。」
rem  原因：旧版安装脚本用 icacls /inheritance:r 重设权限时，只把目录授权给了
rem        Users，文件（exe / DLL）上却没落地，导致普通用户读不到程序文件。
rem  本脚本把 ACL 重置为「继承 ProgramData 默认权限」，再显式授予所有用户
rem  「读取+执行」，文件和目录一起修好。
rem  用法：右键本文件 -^> 以管理员身份运行
rem ============================================================
setlocal
set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=C:\ProgramData\ScreenCaptureTool"

net session >nul 2>nul
if errorlevel 1 (
    echo 请右键本文件 -^> 以管理员身份运行。
    pause
    exit /b 1
)

if not exist "%TARGET%" (
    echo 没有找到目录：%TARGET%
    echo 请先运行「安装给所有用户-需管理员.bat」，或把要修复的目录作为参数传进来。
    pause
    exit /b 1
)

echo 目标目录：%TARGET%
echo.
echo [1/2] 重置为继承的系统默认权限（去掉旧的错误授权）...
icacls "%TARGET%" /reset /T /C
if errorlevel 1 goto :error

echo.
echo [2/2] 授予所有用户「读取+执行」...
icacls "%TARGET%" /grant "*S-1-5-32-545:(OI)(CI)RX" /T /C
if errorlevel 1 goto :error

echo.
echo 完成。现在普通用户也能运行了：
echo   %TARGET%\app\应用窗口定时截图工具.exe
echo 程序目录保持只读是故意的：日志与截图会写在每个用户自己的
echo   %%LOCALAPPDATA%%\ScreenCaptureTool  和  %%USERPROFILE%%\.Screen_Capture 里。
pause
exit /b 0

:error
echo.
echo 修复失败：可能有文件正被占用，请先让所有用户退出程序后重试。
pause
exit /b 1
