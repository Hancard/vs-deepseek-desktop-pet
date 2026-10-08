@echo off
set PYTHON=%AIRI_PYTHON_PATH%
if "%PYTHON%"=="" set PYTHON=python

cd /d "%~dp0."
echo Starting deepseek Desktop Pet...
start "deepseek Pet" "%PYTHON%" standalone.py
