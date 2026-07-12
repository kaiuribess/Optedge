@echo off
REM Launches Optedge on Windows, preferring this repository's virtual environment.
REM Usage: run.bat [Optedge command-line arguments]

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
