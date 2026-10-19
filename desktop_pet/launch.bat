@echo off
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
echo Starting deepseek Desktop Pet...
start "deepseek Pet" "%PYTHON%" standalone.py
