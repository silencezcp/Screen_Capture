@echo off
chcp 936 >nul
rem ============================================================
rem  把本仓库推送到 Gitee：https://gitee.com/silence95/Screen_Capture
rem
rem  第一次使用会弹出登录窗口，请这样填：
rem    用户名：silence95
rem    密码  ：Gitee 私人令牌（推荐，账号密码常被拒）
rem            获取：Gitee 右上角头像 -> 设置 -> 安全设置 -> 私人令牌 -> 生成新令牌
rem            权限勾选 projects 即可，复制那串字符当密码用
rem  成功后 Windows 凭据管理器会记住，以后不用再登录。
rem ============================================================
setlocal
cd /d "%~dp0"

echo ============================================
echo  推送到 Gitee: gitee.com/silence95/Screen_Capture
echo ============================================
echo.

git remote get-url gitee >nul 2>nul
if errorlevel 1 git remote add gitee https://gitee.com/silence95/Screen_Capture.git

echo --- 推送主分支 ---
git push -u gitee main
if errorlevel 1 goto :failed

echo.
echo --- 推送标签 ---
git push gitee --tags

echo.
echo 完成。仓库地址：https://gitee.com/silence95/Screen_Capture
pause
exit /b 0

:failed
echo.
echo 推送失败。常见原因：
echo   1. 登录窗口里用户名/密码填错（用户名必须是 silence95，密码建议用私人令牌）
echo   2. 上一次输错后凭据被系统清掉了 —— 重新运行本脚本，在弹出的窗口里重新填一次即可
echo   3. 如果不想再处理登录：改用 Gitee 的「导入仓库」从 GitHub 同步，不需要任何凭据
echo.
pause
exit /b 1
