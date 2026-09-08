@echo off
rem Build portable artifacts with private source paths remapped.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
exit /b %errorlevel%
