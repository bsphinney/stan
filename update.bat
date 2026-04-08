@echo off
echo Updating STAN...
echo.
powershell -ExecutionPolicy Bypass -File "%~dp0update_stan.ps1"
pause
