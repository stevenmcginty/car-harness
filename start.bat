@echo off
setlocal
cd /d "%~dp0"

REM --- Create the virtual environment on first run ---------------------------
REM NOTE: never put a bare ) inside an if-block echo - it terminates the block.
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Failed to create the virtual environment.
        echo Make sure Python 3 is installed and on your PATH, then try again.
        pause
        exit /b 1
    )
)

REM --- Install dependencies only when something is missing --------------------
".venv\Scripts\python.exe" -c "import uvicorn, fastapi, mcp, bleak, serial" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies - the first run can take a minute...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency installation failed. See the messages above.
        pause
        exit /b 1
    )
)

REM --- Open the app window ONLY once the server actually answers -------------
REM open-app.ps1 opens Chrome/Edge in --app mode: its own window and its own
REM taskbar icon, not a browser tab. Keep the logic in the .ps1 - quoting a
REM PowerShell one-liner inside a batch file is how it used to go wrong.
echo Car Harness will open in its own window once the server is ready...
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-app.ps1"

REM --- Run the server (Ctrl+C to stop) ---------------------------------------
echo Starting car-harness on http://127.0.0.1:8765  ^(press Ctrl+C to stop^)
echo.
".venv\Scripts\python.exe" run.py

echo.
echo ============================================================
echo  The car-harness server has stopped.
echo  If there is an error above, that is the reason it stopped.
echo ============================================================
pause
endlocal
