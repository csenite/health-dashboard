@echo off
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  for %%P in ("%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe") do (
    if exist %%P if not defined PY set "PY=%%~P"
  )
)
if not defined PY (
  echo Python not found. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
  pause
  exit /b 1
)
"%PY%" -m pip install --quiet requests cryptography google-auth
"%PY%" preview.py
pause
