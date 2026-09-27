@echo off
rem Installer helper: creates the bot's own Python (.venv) in this folder and installs the packages.
rem Python is downloaded by uv into the "python" folder here, so no other Python on this PC is touched.
cd /d "%~dp0"
set "UV_PYTHON_INSTALL_DIR=%~dp0python"
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_NO_CACHE=1"
echo [%date% %time%] start> install-log.txt
bin\uv.exe venv --python 3.13 --seed --allow-existing .venv >> install-log.txt 2>&1 || exit /b 1
bin\uv.exe pip install --python .venv\Scripts\python.exe -U -r requirements.txt >> install-log.txt 2>&1 || exit /b 2
echo [%date% %time%] done>> install-log.txt
