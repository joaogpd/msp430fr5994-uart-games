@echo off
setlocal
set "BAUD=115200"
set "MODE=auto"

for %%A in (%*) do (
    if /I "%%~A"=="inline" (
        set "MODE=inline"
    ) else if /I "%%~A"=="putty" (
        set "MODE=putty"
    ) else (
        set "BAUD=%%~A"
    )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch_terminal.ps1" -Baud %BAUD% -Mode %MODE%
