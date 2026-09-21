@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0Organizer.py" "%~dp0."
) else (
    python "%~dp0Organizer.py" "%~dp0."
)
if errorlevel 1 pause
endlocal
