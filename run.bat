@echo off
REM Launches Optedge on Windows, preferring this repository's virtual environment.
REM Usage: run.bat [Optedge command-line arguments]

setlocal EnableDelayedExpansion

set "ROOT=%~dp0"
cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM Make the first launch self-bootstrapping instead of using an unconfigured
REM system Python and failing with missing-package errors.
if not exist "%ROOT%venv\Scripts\python.exe" (
    echo Optedge is not installed in this folder yet. Starting one-time setup...
    set OPTEDGE_NO_PAUSE=1
    call "%ROOT%install.bat"
    if !errorlevel! neq 0 (
        echo.
        echo ERROR: Optedge setup did not complete.
        pause
        exit /b 1
    )
    set OPTEDGE_NO_PAUSE=
)

"%ROOT%venv\Scripts\python.exe" "%ROOT%run.py" %*
set EXIT_CODE=!errorlevel!

endlocal & exit /b %EXIT_CODE%
