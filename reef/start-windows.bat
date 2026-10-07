@echo off
setlocal
cd /d "%~dp0"
title Reef setup

echo === Reef: checking Docker ===
docker info >nul 2>&1
if errorlevel 1 goto nodocker

rem An older setup could leave a FOLDER named .env behind (Docker creates it when the file is missing).
if exist ".env\" rmdir /s /q ".env"

if exist ".env" goto checkkeys
copy /y ".env.example" ".env" >nul
echo.
echo Created the file .env in this folder. Notepad opens it now.
echo Paste your keys right after APIFY_TOKEN= and OPENROUTER_API_KEY=
echo then press Ctrl+S to save and close Notepad.
start /wait notepad ".env"

:checkkeys
call :haskeys
if not errorlevel 1 goto build
echo.
echo APIFY_TOKEN or OPENROUTER_API_KEY is still empty in .env - Notepad opens it again.
echo Paste the keys, press Ctrl+S, close Notepad.
start /wait notepad ".env"
call :haskeys
if errorlevel 1 goto nokeys

:build
echo.
echo === Building Reef - the first time takes a few minutes ===
docker compose build
if errorlevel 1 goto failed

echo.
echo === Checking your keys - a test message goes to Telegram/e-mail if configured ===
docker compose run --rm reef python -m reef doctor --notify
if errorlevel 1 goto doctorfailed

echo.
echo === Starting Reef in the background ===
docker compose up -d
if errorlevel 1 goto failed

echo Waiting 15 seconds to make sure it keeps running...
timeout /t 15 /nobreak >nul
docker compose ps --status running --quiet > "%TEMP%\reef_ps.txt"
for %%A in ("%TEMP%\reef_ps.txt") do if %%~zA==0 goto notrunning

echo.
docker compose exec reef python -m reef status
echo.
echo Reef is running. It starts again by itself whenever Docker Desktop starts.
echo Useful commands - open a terminal in this folder and type:
echo   docker compose logs -f --tail 100                    see what it is doing
echo   docker compose exec reef python -m reef status       fleet overview
echo   docker compose exec reef python -m reef pause        stop switch
echo   docker compose exec reef python -m reef resume       start again
echo.
pause
exit /b 0

:haskeys
powershell -NoProfile -Command "$t = Get-Content -Raw '.env'; if (($t -match '(?m)^APIFY_TOKEN=\S') -and ($t -match '(?m)^OPENROUTER_API_KEY=\S')) { exit 0 } else { exit 1 }"
exit /b %errorlevel%

:nodocker
echo.
echo Docker Desktop is not running. Start Docker Desktop, wait until it shows "Engine running",
echo then double-click this file again.
pause
exit /b 1

:nokeys
echo.
echo The keys are still missing. Open .env in this folder, paste them, save, and run this file again.
pause
exit /b 1

:doctorfailed
echo.
echo Some checks failed - see the lines marked [x] above.
echo Fix the key in .env - Notepad opens it now - save, then double-click this file again.
start notepad ".env"
pause
exit /b 1

:notrunning
echo.
echo Reef stopped right after starting. Its last messages:
docker compose logs --tail 40
echo.
echo Copy the messages above and send them for help.
pause
exit /b 1

:failed
echo.
echo Something went wrong - see the messages above.
pause
exit /b 1
