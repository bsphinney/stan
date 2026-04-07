@echo off
echo Downloading STAN installer...
del "%~dp0install_stan.ps1" >nul 2>&1
powershell -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/bsphinney/stan/main/install_stan.ps1' -OutFile '%~dp0install_stan.ps1' -UseBasicParsing; & '%~dp0install_stan.ps1'"
pause
