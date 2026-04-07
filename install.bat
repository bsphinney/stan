@echo off
echo Downloading STAN installer...
del "%~dp0install_stan.ps1" >nul 2>&1
powershell -ExecutionPolicy Bypass -Command "try{Add-Type 'using System.Net;using System.Net.Security;using System.Security.Cryptography.X509Certificates;public class TrustAll{public static void Enable(){ServicePointManager.ServerCertificateValidationCallback=delegate{return true;};}}';[TrustAll]::Enable()}catch{}; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $t=[DateTime]::Now.Ticks; Invoke-WebRequest -Uri \"https://raw.githubusercontent.com/bsphinney/stan/main/install_stan.ps1?t=$t\" -OutFile '%~dp0install_stan.ps1' -UseBasicParsing; & '%~dp0install_stan.ps1'"
pause
