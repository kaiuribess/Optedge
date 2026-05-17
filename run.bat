@echo off
REM Optedge v20 - run launcher (Windows)
REM Usage: run.bat               -> standard run
REM        run.bat --aggressive --bankroll 25000 --loop 30   -> user's daily command

setlocal EnableDelayedExpansion

set ROOT=%~dp0
set PYTHONPATH=%ROOT%;%PYTHONPATH%
set PYTHONIOENCODING=utf-8

REM Prefer venv if present
if exist "%ROOT%venv\Scripts\python.exe" (
    set PY="%ROOT%venv\Scripts\python.exe"
) else (
    set PY=python
)

%PY% "%ROOT%run.py" %*

endlocal
