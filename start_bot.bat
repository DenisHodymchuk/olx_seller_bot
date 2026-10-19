@echo off
chcp 65001 >nul
echo Запуск Telegram-бота для OLX (MakerWorld)...
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%~dp0bot.py"
pause
