@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo 🤖 AI 信息差搬运工 — 每日一条
echo =====================================
echo.
echo 请确保 VPN 已开启！
echo.
pause
echo.
echo 🚀 开始生成今日内容...
echo.
python pipeline.py
pause
