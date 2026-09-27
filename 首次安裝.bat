@echo off
rem First-time setup: installs Python, packages, ffmpeg and optional AI/search. Safe to run again.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
pause
