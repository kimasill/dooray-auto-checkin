@echo off
REM Thin wrapper. All logic and Korean text live in install.ps1 so that
REM console codepage issues never mangle the messages.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
