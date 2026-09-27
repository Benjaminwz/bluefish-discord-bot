@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ====================================
echo   Installing required packages...
echo ====================================
pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo Package installation failed. Make sure Python is installed and you have an internet connection.
    pause
    exit /b 1
)

echo.
echo ====================================
echo   Starting the bot...
echo ====================================
python deepseek_discord_bot.py

echo.
echo The bot has stopped.
pause
