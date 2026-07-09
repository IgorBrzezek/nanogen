@echo off
set "CHROME_PATH="

if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
) else (
    echo Chrome not found in standard locations. Please edit this script to set the path.
    pause
    exit /b
)

echo Closing all Chrome instances...
taskkill /F /IM chrome.exe /T >nul 2>&1

echo Starting Chrome in Remote Debugging Mode (Port 9222)...
start "" "%CHROME_PATH%" --remote-debugging-port=9222 --user-data-dir="C:\chrome_dev_profile" https://gemini.google.com/

echo.
echo Chrome started. You can now run the Python script.
echo.
pause
