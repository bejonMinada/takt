@echo off
REM Launches Takt with fabricated presentation data, fully separate from any
REM real captured data. Safe to run any time - never touches %LOCALAPPDATA%\Takt.
cd /d "%~dp0"
if not exist venv (
    echo First run: creating virtual environment and installing dependencies...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)
pythonw demo.py
