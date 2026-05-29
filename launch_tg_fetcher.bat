@echo off
setlocal

set "APP_DIR=%~dp0"
set "APP_URL=http://127.0.0.1:5151/"

if not exist "%APP_DIR%\server.py" (
    echo server.py not found in %APP_DIR%
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing '%APP_URL%' | Out-Null; Start-Process '%APP_URL%'; exit 0 } catch { exit 1 }"
if %errorlevel%==0 exit /b 0

cd /d "%APP_DIR%"

where py >nul 2>nul
if %errorlevel%==0 (
    start "TG Fetcher" cmd /k "cd /d ""%APP_DIR%"" && py -3 server.py"
) else (
    start "TG Fetcher" cmd /k "cd /d ""%APP_DIR%"" && python server.py"
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline=(Get-Date).AddSeconds(30); while((Get-Date) -lt $deadline){ try { Invoke-WebRequest -UseBasicParsing '%APP_URL%' | Out-Null; Start-Process '%APP_URL%'; exit 0 } catch { Start-Sleep -Seconds 1 } }; Start-Process '%APP_URL%'"

endlocal
