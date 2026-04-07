@echo off
echo Updating STAN...
call "%USERPROFILE%\.stan\venv\Scripts\activate.bat"
pip install --no-cache-dir --force-reinstall --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host github.com --trusted-host objects.githubusercontent.com https://github.com/bsphinney/stan/archive/main.zip
echo.
echo Updated. Starting dashboard...
stan dashboard
pause
