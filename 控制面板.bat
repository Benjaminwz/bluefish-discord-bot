@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    rem Python not found: run the first-time setup instead of the Microsoft Store "python" alias
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
    exit /b
)
start "" pythonw panel.pyw
