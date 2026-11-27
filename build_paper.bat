@echo off
setlocal EnableDelayedExpansion
set "ARGS="
for %%A in (%*) do (
    if /I "%%~A"=="open" (
        set "ARGS=!ARGS! -Open"
    ) else if /I "%%~A"=="keep" (
        set "ARGS=!ARGS! -KeepAux"
    )
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_paper.ps1" !ARGS!
