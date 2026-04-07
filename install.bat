@echo off
echo Downloading STAN installer...
powershell -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/bsphinney/stan/main/install_stan.ps1' -OutFile '%TEMP%\install_stan.ps1' -UseBasicParsing; & '%TEMP%\install_stan.ps1'"
pause
