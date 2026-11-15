@echo off
setlocal
set "ROOT=%~dp0.."
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
    "%VENV_PY%" -m spoolwarden_vision_server.service.cli update-models %*
) else (
    python -m spoolwarden_vision_server.service.cli update-models %*
)
if errorlevel 1 exit /b %ERRORLEVEL%
exit /b 0
