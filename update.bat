@echo off
echo Updating STAN...
call "%USERPROFILE%\.stan\venv\Scripts\activate.bat"
pip install --force-reinstall https://github.com/bsphinney/stan/archive/main.zip
echo.
echo Updated. Starting dashboard...
stan dashboard
pause
