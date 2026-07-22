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

REM --- Open the browser ONLY once the server actually answers ----------------
echo The browser will open automatically once the server is ready...
start "" /b powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){try{$null=Invoke-WebRequest 'http://127.0.0.1:8765/api/profiles' -UseBasicParsing -TimeoutSec 1; break}catch{Start-Sleep -Milliseconds 500}}; Start-Process 'http://127.0.0.1:8765'"

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
