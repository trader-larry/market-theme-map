@echo off
REM Market Theme Map - double-click to start. Add --demo for made-up data.
REM Uses only Python's built-in libraries, so Smart App Control has no add-on DLLs to block.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% --version >nul 2>nul || goto :nopython
%PY% app.py %*
pause
exit /b

:nopython
echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
exit /b 1
