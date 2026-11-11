@echo off
REM ===========================================================
REM  qoder2api-public supervisor (thin wrapper)
REM
REM  Usage: start-qoder-public.bat start|stop|restart|status|run
REM
REM  All real logic lives in qoder_public_runtime.py; this file
REM  only locates Python and forwards arguments.
REM ===========================================================
setlocal
set "HERE=%~dp0"
set "SCRIPT=%HERE%qoder_public_runtime.py"

if "%~1"=="" (
    echo Usage: %~nx0 start^|stop^|restart^|status^|run [--config PATH]
    exit /b 2
)

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found on PATH.
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo [ERROR] %SCRIPT% not found.
    exit /b 1
)

python "%SCRIPT%" %*
exit /b %ERRORLEVEL%
