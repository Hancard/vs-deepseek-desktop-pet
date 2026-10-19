@echo off
chcp 65001 >nul
set PYTHON=%AIRI_PYTHON_PATH%
if "%PYTHON%"=="" set PYTHON=python
where "%PYTHON%" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found: %PYTHON%
  echo         Set AIRI_PYTHON_PATH to your python.exe and try again.
  pause
  exit /b 1
)

cd /d "%~dp0."
echo ============================================
echo   deepseek Desktop Pet - full launch (pet + watcher)
echo ============================================
echo.

echo [1/2] Starting deepseek pet window...
start "deepseek Pet" "%PYTHON%" standalone.py
timeout /t 3 /nobreak >nul

echo [2/2] Starting file watcher...
echo.
echo   deepseek is now watching your code.
echo   Edit .py / .c / .cpp files in this project.
echo   Save a file with errors to see deepseek's reaction!
echo.
echo   Close deepseek's window or press Ctrl+C to stop.
echo ============================================
"%PYTHON%" -u watcher.py --dir ..
pause
