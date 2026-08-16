@echo off
rem Build everything: sttt.dll (Python bridge backend) + SuperTicTacToe.exe (pure Rust app)
cd /d "%~dp0"
echo [1/2] cargo build --release (core cdylib)...
cargo build --release || (echo [ERROR] core build failed & pause & exit /b 1)
echo [2/2] cargo build --release (wry app)...
cd app
cargo build --release || (echo [ERROR] app build failed & pause & exit /b 1)
copy /y target\release\sttt-app.exe ..\..\SuperTicTacToe.exe >nul
copy /y target\release\sttt.dll ..\..\super_ttt\sttt.dll >nul
echo [OK] ..\SuperTicTacToe.exe + super_ttt\sttt.dll updated
