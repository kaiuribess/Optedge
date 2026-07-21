@echo off
REM Installs Optedge and its dependencies into a local virtual environment on Windows.
REM Usage: double-click install.bat OR run from cmd: install.bat

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo.
echo +======================================+
echo ^|   Optedge - easy setup ^(Windows^)     ^|
echo +======================================+
echo.

REM Step 1: detect compatible Python
set PY_CMD=
for %%V in (3.12 3.13 3.11) do (
    if "!PY_CMD!"=="" (
        py -%%V --version >nul 2>&1 && set PY_CMD=py -%%V
    )
)
if "%PY_CMD%"=="" (
    python --version >nul 2>&1
    if !errorlevel! equ 0 (
        for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
        echo Found python !PY_VER!
        set PY_CMD=python
    )
)
if "%PY_CMD%"=="" (
    echo.
    echo ERROR: No compatible Python found ^(need 3.11 - 3.13^)
    echo        Install Python 3.12 from https://www.python.org/downloads/
    echo        Make sure "Add python.exe to PATH" is CHECKED during install.
    if not defined OPTEDGE_NO_PAUSE pause
    exit /b 1
)

%PY_CMD% -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13)) else 1)" >nul 2>&1
if !errorlevel! neq 0 (
    echo.
    echo ERROR: Optedge requires Python 3.11 through 3.13.
    echo        Install Python 3.12 or 3.13 and run this installer again.
    if not defined OPTEDGE_NO_PAUSE pause
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
        if not defined OPTEDGE_NO_PAUSE pause
        exit /b 1
    )
)

venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13)) else 1)" >nul 2>&1
if !errorlevel! neq 0 (
    echo.
    echo ERROR: The existing venv is broken or uses an unsupported Python.
    echo        Remove the venv directory and run this installer again.
    if not defined OPTEDGE_NO_PAUSE pause
    exit /b 1
)

REM Activate venv
call venv\Scripts\activate.bat

REM Step 3: install deps
echo Installing dependencies ^(a cold install can take 2-5 minutes^)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if !errorlevel! neq 0 (
    echo.
    echo ERROR: pip install failed. Check requirements.txt and your internet.
    if not defined OPTEDGE_NO_PAUSE pause
    exit /b 1
)
python -m pip check
if !errorlevel! neq 0 (
    echo.
    echo ERROR: Installed dependencies are inconsistent.
    if not defined OPTEDGE_NO_PAUSE pause
    exit /b 1
)
echo [OK] Dependencies installed
echo.

REM Step 4: run setup check
echo Running offline setup health check...
echo.
python setup_check.py --offline
if !errorlevel! neq 0 (
    echo.
    echo ERROR: Offline setup health check failed.
    if not defined OPTEDGE_NO_PAUSE pause
    exit /b 1
)

REM Step 5: print next steps
echo.
echo +======================================+
echo ^|   Setup complete                     ^|
echo +======================================+
echo.
echo Run the pipeline:           run.bat
echo Demo mode ^(no network^):    run.bat --demo
echo Fast insider:               run.bat --fast-insider
echo Open the Trade Desk:        run.bat --cockpit
echo Double-click cockpit:       start_cockpit.bat
echo.
echo Before the first live scan, set OPTEDGE_CONTACT and run:
echo    venv\Scripts\python.exe setup_check.py
echo.
echo run.bat automatically uses this folder's private Python environment.
echo.
echo Outputs land in: data\
echo   - dashboard_*.html      ^(open in browser^)
echo   - tradingview_watchlist_*.txt ^(import in TradingView^)
echo.
if not defined OPTEDGE_NO_PAUSE pause
