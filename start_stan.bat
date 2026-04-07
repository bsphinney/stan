@echo off
echo Starting STAN...
call "%USERPROFILE%\.stan\venv\Scripts\activate.bat"
start "STAN Dashboard" cmd /c "stan dashboard"
timeout /t 3 >nul
start http://localhost:8421
stan watch
pause
