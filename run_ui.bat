@echo off
cd /d %~dp0
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
python -m pip install -r requirements.txt
python app.py
pause
