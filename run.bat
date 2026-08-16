@echo off
cd /d "%~dp0"
if not exist venv (
    echo First run: creating virtual environment and installing dependencies...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)
python tray.py
