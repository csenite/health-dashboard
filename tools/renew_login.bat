@echo off
rem Double-click to renew the Google Health login and update GitHub automatically.
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

rem GitHub CLI: install it the first time, then sign in once
set "GH=gh"
where gh >nul 2>nul || (
  if exist "C:\Program Files\GitHub CLI\gh.exe" (set "GH=C:\Program Files\GitHub CLI\gh.exe") else (
    echo Installing GitHub CLI ^(one time^)...
    winget install --id GitHub.cli -e --silent --accept-package-agreements --accept-source-agreements
    set "GH=C:\Program Files\GitHub CLI\gh.exe"
  )
)
"%GH%" auth status >nul 2>nul || (
  echo.
  echo One-time GitHub sign-in: press Enter at each question, then approve in the browser.
  "%GH%" auth login --hostname github.com --git-protocol https --web --scopes "repo,workflow"
)

"%PY%" -m pip install --quiet google-auth-oauthlib
"%PY%" renew_login.py
echo.
pause
