@echo off
chcp 65001 >nul
cd /d "%~dp0desktop_pet"

rem ---- resolve a usable Python interpreter -------------------------------
rem AIRI_PYTHON_PATH first, then whatever is on PATH. Every candidate is
rem checked with `where` before use: without it a missing interpreter just
rem prints "'python' is not recognized" inside the spawned window and the
rem user never learns which step actually failed.
set PYTHON=%AIRI_PYTHON_PATH%
if not "%PYTHON%"=="" goto :have
set PYTHON=python
where "%PYTHON%" >nul 2>&1
if not errorlevel 1 goto :have
set PYTHON=python3
where "%PYTHON%" >nul 2>&1
if not errorlevel 1 goto :have
set PYTHON=py
where "%PYTHON%" >nul 2>&1
if not errorlevel 1 goto :have
echo [ERROR] No Python interpreter found.
echo         Install Python 3.10+ (and pip install pywebview), or set
echo         AIRI_PYTHON_PATH to your python.exe and run this again.
pause
exit /b 1

:have
echo ============================================
echo   deepseek Desktop Pet - one-click launch
echo ============================================
echo.
echo   deepseek will watch your project for errors
echo   when you save Python/C/C++ files.
echo.
echo   Python: %PYTHON%
echo   Close this window or press Ctrl+C to stop.
echo ============================================
echo.

echo [1/2] Starting deepseek pet window...
start "deepseek Pet" "%PYTHON%" standalone.py
timeout /t 3 /nobreak >nul

echo [2/2] Starting file watcher...
"%PYTHON%" -u watcher.py --dir ..

pause
