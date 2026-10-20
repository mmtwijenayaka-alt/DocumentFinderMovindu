@echo off
setlocal
set "APP_DIR=%~dp0"
set "APP_FILE=%APP_DIR%optimized_docfinder.py"
set "DATA_DIR=%LOCALAPPDATA%\DocFinder"
set "VENV_DIR=%DATA_DIR%\venv"

if not exist "%APP_FILE%" (
  echo Cannot find optimized_docfinder.py beside this launcher.
  pause
  exit /b 1
)
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
set "DOCFINDER_DATA_DIR=%DATA_DIR%"
rem If cloudflared.exe is beside the app, the app may open a public HTTPS quick tunnel.

if not exist "%VENV_DIR%\Scripts\python.exe" (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python 3 is not installed or the Python launcher is unavailable.
    echo Ask IT to install Python 3, or install it for your Windows user if permitted.
    pause
    exit /b 1
  )
  py -3 -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo Could not create the local Python environment.
    pause
    exit /b 1
  )
)

if not exist "%VENV_DIR%\packages-installed.flag" (
  "%VENV_DIR%\Scripts\python.exe" -m pip install Flask waitress pypdf PyMuPDF
  if errorlevel 1 (
    echo Package installation failed. Check internet access or ask IT for help.
    pause
    exit /b 1
  )
  echo ready>"%VENV_DIR%\packages-installed.flag"
)

"%VENV_DIR%\Scripts\python.exe" "%APP_FILE%"
pause
