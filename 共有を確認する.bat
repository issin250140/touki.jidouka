@echo off
chcp 65001 >nul
cd /d "%~dp0"
python sync_sharing.py --check
echo.
pause
