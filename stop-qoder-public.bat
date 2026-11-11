@echo off
REM ===========================================================
REM  qoder2api-public supervisor stop (thin wrapper)
REM
REM  Usage: stop-qoder-public.bat [--config PATH]
REM  (also accepts start|restart|status|run for convenience;
REM   the .bat pair is symmetric on purpose)
REM ===========================================================
setlocal
set "HERE=%~dp0"
set "SCRIPT=%HERE%qoder_public_runtime.py"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found on PATH.
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo [ERROR] %SCRIPT% not found.
    exit /b 1
)

if "%~1"=="" (
    python "%SCRIPT%" stop
) else (
    python "%SCRIPT%" %*
)
exit /b %ERRORLEVEL%
