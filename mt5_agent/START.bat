@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title MT5 Bot Worker

echo.
echo ============================================================
echo   MT5 Bot Worker — Automatisk opsætning
echo ============================================================
echo.

:: ── Tjek Python ─────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo Python er ikke installeret!
    echo.
    echo Åbner download-siden for Python ...
    start https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
    echo.
    echo 1. Download og installer Python fra linket der åbnede
    echo 2. VIGTIGT: Sæt hak ved "Add Python to PATH"
    echo 3. Genstart computeren
    echo 4. Dobbeltklik START.bat igen
    echo.
    pause
    exit /b 1
)
echo Python fundet ✓

:: ── Installer pakker ────────────────────────────────────────
echo.
echo Installerer pakker (første gang tager et minut) ...
pip install MetaTrader5 "redis[asyncio]" python-dotenv --quiet --upgrade
if errorlevel 1 (
    echo.
    echo FEJL: Kunne ikke installere pakker.
    echo Højreklik på START.bat og vælg "Kør som administrator".
    pause
    exit /b 1
)
echo Pakker OK ✓

:: ── Opret .env hvis den ikke findes ─────────────────────────
if not exist ".env" (
    echo.
    echo ════════════════════════════════════════════════════════
    echo  Første gang: du skal bruge din Railway Redis URL
    echo.
    echo  Sådan finder du den:
    echo  1. Gå til railway.app og log ind
    echo  2. Klik på dit projekt
    echo  3. Klik på Redis-tjenesten
    echo  4. Klik på fanebladet "Connect"
    echo  5. Kopier URL der starter med  redis://
    echo ════════════════════════════════════════════════════════
    echo.
    set /p REDIS_URL="Indsæt Redis URL og tryk Enter: "
    if "!REDIS_URL!"=="" (
        echo Ingen URL — prøv igen.
        pause
        exit /b 1
    )
    (
        echo REDIS_URL=!REDIS_URL!
        echo MT5_LOT_SIZE=0.01
        echo MT5_MAGIC=777000
    ) > .env
    echo.
    echo URL gemt i .env ✓  ^(behøver ikke gøres igen^)
)

:: ── Start worker med auto-restart ────────────────────────────
:loop
echo.
echo ════════════════════════════════════════════════════════
echo  Worker starter — lad dette vindue være åbent!
echo  Din bot sender en Telegram-besked når den er klar.
echo ════════════════════════════════════════════════════════
echo.
python mt5_worker.py
echo.
echo Worker stoppede uventet — genstarter om 10 sekunder ...
timeout /t 10 /nobreak >nul
goto loop
