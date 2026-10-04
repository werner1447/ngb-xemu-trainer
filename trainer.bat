@echo off
rem Starts the trainer:  trainer.bat [gui|info|scan|demo]
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The virtual environment is missing. Run once:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
rem The window runs without a console (pythonw); info and scan need one.
if "%~1"=="" goto window
if /i "%~1"=="gui" goto window
if /i "%~1"=="demo" goto window
".venv\Scripts\python.exe" -m ngbtrainer %*
if errorlevel 1 pause
exit /b

:window
start "" ".venv\Scripts\pythonw.exe" -m ngbtrainer %*
