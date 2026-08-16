@echo off
REM Run this ONCE, as Administrator, to make Takt start automatically at
REM every login with the permissions it needs to read the Security log.
REM Right-click this file -> "Run as administrator".

set APPDIR=%~dp0
set PYW=%APPDIR%tray.py

echo Creating scheduled task "Takt" (runs at logon, highest privileges)...

schtasks /Create /TN "Takt" ^
  /TR "pythonw \"%PYW%\"" ^
  /SC ONLOGON ^
  /RL HIGHEST ^
  /F

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Done. Takt will now start automatically next time you log in.
    echo You can start it right now too:
    schtasks /Run /TN "Takt"
) else (
    echo.
    echo Could not create the scheduled task. This usually means you are not
    echo running as Administrator, or your account cannot create elevated
    echo tasks - check with IT. Takt can still be run manually with
    echo run.bat in the meantime.
)
pause
