@echo off
cd /d "%~dp0harness"
call .venv\Scripts\activate
python main.py %*
