@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0harness"
call .venv\Scripts\activate
python main.py %*
