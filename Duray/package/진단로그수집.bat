@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect-diagnostics.ps1"
if errorlevel 1 echo Diagnostics failed. Please send the error shown above.
echo.
pause
