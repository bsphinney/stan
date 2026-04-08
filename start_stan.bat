@echo off
echo Starting STAN...
if exist "%USERPROFILE%\STAN\venv\Scripts\activate.bat" (
    call "%USERPROFILE%\STAN\venv\Scripts\activate.bat"
) else (
    call "%USERPROFILE%\.stan\venv\Scripts\activate.bat"
)
start "STAN Dashboard" cmd /c "stan dashboard"
timeout /t 3 >nul
start http://localhost:8421
stan watch
pause
