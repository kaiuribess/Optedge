@echo off
REM Optedge - Windows easy installer
REM Usage: double-click install.bat OR run from cmd: install.bat

setlocal EnableDelayedExpansion

echo.
echo +======================================+
echo ^|   Optedge - easy setup ^(Windows^)     ^|
echo +======================================+
echo.

REM Step 1: detect compatible Python
set PY_CMD=
for %%V in (3.13 3.12 3.11 3.10 3.9) do (
    if "!PY_CMD!"=="" (
        py -%%V --version >nul 2>&1 && set PY_CMD=py -%%V
    )
)
if "%PY_CMD%"=="" (
    python --version >nul 2>&1
    if !errorlevel! equ 0 (
        for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
        echo Found python !PY_VER!
        REM Refuse Python 3.14+ (no wheels yet for data-science packages)
        echo !PY_VER! | findstr /b "3.14" >nul && (
            echo.
            echo ERROR: Python 3.14 has no Windows wheels for the packages we need.
            echo        Install Python 3.13 from https://www.python.org/downloads/release/python-3130/
            echo        Make sure "Add python.exe to PATH" is CHECKED during install.
            pause
            exit /b 1
        )
        set PY_CMD=python
    )
)
if "%PY_CMD%"=="" (
    echo.
    echo ERROR: No compatible Python found ^(need 3.9 - 3.13^)
    echo        Install Python 3.13 from https://www.python.org/downloads/release/python-3130/
    echo        Make sure "Add python.exe to PATH" is CHECKED during install.
    pause
    exit /b 1
)

echo [OK] Using: %PY_CMD%
%PY_CMD% --version
echo.

REM Step 2: create venv
if not exist venv (
    echo Creating virtual environment...
    %PY_CMD% -m venv venv
    if !errorlevel! neq 0 (
        echo ERROR: venv creation failed
        pause
        exit /b 1
    )
)

REM Activate venv
call venv\Scripts\activate.bat

REM Step 3: install deps
echo Installing dependencies ^(this takes 30-60 seconds^)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if !errorlevel! neq 0 (
    echo.
    echo ERROR: pip install failed. Check requirements.txt and your internet.
    pause
    exit /b 1
)
echo [OK] Dependencies installed
echo.

REM Step 4: run setup check
echo Running setup health check...
echo.
python setup_check.py

REM Step 5: print next steps
echo.
echo +======================================+
echo ^|   Setup complete                     ^|
echo +======================================+
echo.
echo Run the pipeline:           python run.py
echo Demo mode ^(no network^):    python run.py --demo
echo Fast insider:               python run.py --fast-insider
echo.
echo The venv is now active in this window. To re-activate later:
echo    venv\Scripts\activate.bat
echo.
echo Outputs land in: data\
echo   - dashboard_*.html      ^(open in browser^)
echo   - tradingview_watchlist_*.txt ^(import in TradingView^)
echo.
pause
