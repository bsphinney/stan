@echo off
echo Updating STAN...
echo.
echo Downloading latest updater...
REM Delete any cached update_stan.ps1 BEFORE download so a download
REM failure can't silently fall back to a stale version (Exploris
REM 2026-04-14 regression — cached v0.2.93 kept aborting on benign
REM pip warnings long after v0.2.94 was on GitHub).
del "%~dp0update_stan.ps1" 2>nul
powershell -ExecutionPolicy Bypass -Command "try{Add-Type 'using System.Net;using System.Net.Security;using System.Security.Cryptography.X509Certificates;public class TrustAllUpd{public static void Enable(){ServicePointManager.ServerCertificateValidationCallback=delegate{return true;};}}';[TrustAllUpd]::Enable()}catch{}; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $t=[DateTime]::Now.Ticks; try { Invoke-WebRequest -Uri \"https://raw.githubusercontent.com/bsphinney/stan/main/update_stan.ps1?t=$t\" -OutFile '%~dp0update_stan.ps1' -UseBasicParsing -ErrorAction Stop } catch { Write-Host 'FATAL: could not download update_stan.ps1 from GitHub — aborting to avoid running stale code.' -ForegroundColor Red; exit 1 }; if (-not (Test-Path '%~dp0update_stan.ps1')) { Write-Host 'FATAL: update_stan.ps1 missing after download attempt.' -ForegroundColor Red; exit 1 }; & '%~dp0update_stan.ps1'"
pause
