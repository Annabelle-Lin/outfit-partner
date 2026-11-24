@echo off
chcp 65001 >nul
title 今遂穿搭助手 · 本地服务 8300
cd /d "%~dp0"

echo ================================================
echo   今遂穿搭助手 - 合并版
echo   启动中... 请稍等，看到 "Uvicorn running" 就是好了
echo ================================================
echo.

"C:\Users\86151\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8300

echo.
echo 服务已停止。按任意键关闭窗口。
pause >nul
