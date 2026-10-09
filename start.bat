@echo off
chcp 65001 >nul
cd /d "%~dp0desktop_pet"
set PYTHON=%AIRI_PYTHON_PATH%
if "%PYTHON%"=="" set PYTHON=C:\Users\Mr.hancard\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PYTHON%" set PYTHON=python

echo ============================================
echo   deepseek Desktop Pet - one-click launch
echo ============================================
echo.
echo   deepseek will watch your project for errors
echo   when you save Python/C/C++ files.
echo.
echo   Close this window or press Ctrl+C to stop.
echo ============================================
echo.

echo [1/2] Starting deepseek pet window...
start "deepseek Pet" "%PYTHON%" standalone.py
timeout /t 3 /nobreak >nul

echo [2/2] Starting file watcher...
"%PYTHON%" -u watcher.py --dir ..

pause
