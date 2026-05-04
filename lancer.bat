@echo off
title Scraping Refs Archi
cd /d "%~dp0"

echo.
echo ===============================================
echo    Scraping Refs Archi
echo    Demarrage du serveur local...
echo ===============================================
echo.

start "" /min cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8000"

echo Serveur sur http://localhost:8000
echo Pour arreter : ferme cette fenetre ou Ctrl+C
echo.

uvicorn server:app --port 8000

echo.
echo Serveur arrete.
pause
