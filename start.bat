@echo off
rem SuperTicTacToe launcher: prefer the pure-Rust app (no Python needed);
rem fall back to the pywebview shell if the exe is missing.
cd /d "%~dp0"
if exist SuperTicTacToe.exe (
  start "" SuperTicTacToe.exe
  exit /b 0
)
where pythonw >nul 2>nul
if errorlevel 1 (
  echo [ERROR] SuperTicTacToe.exe not found and pythonw not found.
  echo Run rust\build.cmd to build the app, or install Python.
  pause
  exit /b 1
)
start "" pythonw "%~dp0main.pyw"
