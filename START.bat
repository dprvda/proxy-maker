@echo off
title Proxy Maker
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python was not found.
  echo  Install it from https://www.python.org/downloads/
  echo  IMPORTANT: tick "Add python.exe to PATH" during install.
  echo.
  pause
  exit /b
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo  ffmpeg was not found.
  echo  Install it, then close and reopen, and run again:
  echo.
  echo      winget install --id Gyan.FFmpeg -e
  echo.
  pause
  exit /b
)

python "%~dp0make_proxies.py"
if errorlevel 1 (
  echo.
  echo  The program exited with an error. See the message above.
  pause
)
