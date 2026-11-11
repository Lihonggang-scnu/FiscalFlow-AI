@echo off
chcp 65001 >nul
cd /d "%~dp0app"
python scripts\launch_workbench.py chat
if errorlevel 1 pause
