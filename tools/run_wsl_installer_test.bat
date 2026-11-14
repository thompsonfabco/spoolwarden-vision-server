@echo off
setlocal
set "ROOT=%~dp0.."
for /f "delims=" %%I in ('wsl wslpath -u "%ROOT%"') do set "WSL_ROOT=%%I"
wsl -e bash "%WSL_ROOT%/tests/wsl_test_installer.sh" %*
exit /b %ERRORLEVEL%
