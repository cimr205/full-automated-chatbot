@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title MT4 Bot Worker

echo.
echo ============================================================
echo   MT4 Bot Worker — Automatisk opsætning
echo ============================================================
echo.

:: ── Tjek Python ─────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo Python er ikke installeret!
    echo.
    start https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
    echo 1. Download og installer Python fra linket der åbnede
    echo 2. VIGTIGT: Sæt hak ved "Add Python to PATH"
    echo 3. Genstart computeren og dobbeltklik START.bat igen
    pause
    exit /b 1
)
echo Python fundet ✓

:: ── Installer pakker ────────────────────────────────────────
echo.
echo Installerer pakker ...
pip install "redis[asyncio]" python-dotenv --quiet --upgrade
if errorlevel 1 (
    echo FEJL: Kunne ikke installere pakker. Kør som administrator.
    pause
    exit /b 1
)
echo Pakker OK ✓

:: ── Opret .env hvis den ikke findes ─────────────────────────
if not exist ".env" (
    echo.
    echo ════════════════════════════════════════════════════════
    echo  Leder efter dine MetaTrader 4-terminaler ...
    echo ════════════════════════════════════════════════════════
    echo.
    set count=0
    for /d %%D in ("%APPDATA%\MetaQuotes\Terminal\*") do (
        if exist "%%D\MQL4\Files" (
            set /a count+=1
            set "FOUND_!count!=%%D\MQL4\Files"
            echo  !count!. %%D\MQL4\Files
        )
    )

    if !count!==0 (
        echo  Ingen MT4-terminal fundet automatisk.
        echo  Åbn MetaTrader 4 → File → "Open Data Folder" → MQL4 → Files
        echo  og kopier stien herfra.
        echo.
        set /p MT4PATH="Indsæt MQL4\Files sti og tryk Enter: "
    ) else (
        echo.
        set /p CHOICE="Vælg nummer ^(eller indsæt sti manuelt^): "
        if defined FOUND_!CHOICE! (
            call set "MT4PATH=%%FOUND_!CHOICE!%%"
        ) else (
            set "MT4PATH=!CHOICE!"
        )
    )

    if "!MT4PATH!"=="" (
        echo Ingen sti angivet — prøv igen.
        pause
        exit /b 1
    )

    echo.
    echo ════════════════════════════════════════════════════════
    echo  Næste: din Railway Redis URL
    echo  ^(Railway → Redis-tjenesten → "Connect" fanen → kopier redis://...^)
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
        echo MT4_FILES_PATH=!MT4PATH!
        echo MT4_LOT_SIZE=0.01
        echo MT4_MAGIC=777000
    ) > .env
    echo .env gemt ✓

    :: ── Kopier Expert Advisor til Experts-mappen ──────────────
    set "EXPERTS_DIR=!MT4PATH:Files=Experts!"
    if exist "!EXPERTS_DIR!" (
        copy /Y "MT4_Bridge.mq4" "!EXPERTS_DIR!\MT4_Bridge.mq4" >nul
        echo MT4_Bridge.mq4 kopieret til Experts-mappen ✓
        echo.
        echo ════════════════════════════════════════════════════════
        echo  SIDSTE TRIN — gør dette i MetaTrader 4:
        echo  1. Tryk F4 ^(eller Tools → MetaEditor^)
        echo  2. Find MT4_Bridge.mq4 i Experts og tryk Compile ^(F7^)
        echo  3. Gå tilbage til MT4, åbn Navigator ^(Ctrl+N^)
        echo  4. Træk MT4_Bridge ind på et åbent chart
        echo  5. Sæt hak ved "Allow live trading" i popup-vinduet
        echo ════════════════════════════════════════════════════════
        echo.
        pause
    ) else (
        echo ADVARSEL: Kunne ikke finde Experts-mappen automatisk.
        echo Kopier MT4_Bridge.mq4 manuelt til MQL4\Experts og compile den i MetaEditor.
        pause
    )
)

:: ── Start worker med auto-restart ────────────────────────────
:loop
echo.
echo ════════════════════════════════════════════════════════
echo  Worker starter — lad dette vindue være åbent!
echo  Sørg for MT4_Bridge er tilknyttet et chart i MetaTrader 4.
echo ════════════════════════════════════════════════════════
echo.
python mt4_worker.py
echo.
echo Worker stoppede uventet — genstarter om 10 sekunder ...
timeout /t 10 /nobreak >nul
goto loop
