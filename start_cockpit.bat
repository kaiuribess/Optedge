@echo off
REM One-click Windows entry point. run.bat installs Optedge on the first launch.

cd /d "%~dp0"
call "%~dp0run.bat" --cockpit
if errorlevel 1 (
    echo.
    echo Optedge stopped with an error. Review the message above.
    pause
)
