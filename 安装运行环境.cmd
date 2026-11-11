@echo off
chcp 65001 >nul
cd /d "%~dp0app"
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Installation failed. Please check Python 3.12 or 3.13 and network access.
  pause
  exit /b 1
)
echo Installation complete. You can now start either workbench.
pause
