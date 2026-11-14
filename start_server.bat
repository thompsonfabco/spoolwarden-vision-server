@echo off
setlocal
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [start_server.bat] Virtual environment not found. Running install.bat first...
    call "%ROOT%install.bat"
    exit /b %ERRORLEVEL%
)

set "FVS_BAT_ROOT=%ROOT%"
set "FVS_BAT_ARGS=%*"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = $env:FVS_BAT_ROOT.TrimEnd('\'); " ^
  "$venvPy = Join-Path $root '.venv\Scripts\python.exe'; " ^
  "$cfgXml = Join-Path $root 'configs\service_config.xml'; " ^
  "$bind = '127.0.0.1'; $port = 7920; $tier = 'auto'; " ^
  "if (Test-Path $cfgXml) { try { [xml]$x = Get-Content $cfgXml; $a = $x.'service-config'; if ($a.host) { $bind = $a.host }; if ($a.port) { $port = [int]$a.port }; if ($a.tier) { $tier = $a.tier } } catch {} }; " ^
  "$hHost = if ($bind -eq '0.0.0.0' -or $bind -eq '::') { '127.0.0.1' } else { $bind }; " ^
  "$url = 'http://' + $hHost + ':' + $port; " ^
  "$alreadyUp = $false; " ^
  "try { $r = Invoke-RestMethod -Uri ($url + '/v1/health') -TimeoutSec 2; if ($r.status) { $alreadyUp = $true } } catch {}; " ^
  "if (-not $alreadyUp) { " ^
  "  New-Item -ItemType Directory -Path (Join-Path $root 'data\logs') -Force | Out-Null; " ^
  "  $env:FILAMENT_VISION_SERVER_HOME = $root; " ^
  "  $p = Start-Process -FilePath $venvPy -ArgumentList @('-m','filament_vision_server.service.cli','serve','--host',$bind,'--port',\"$port\",'--tier',$tier) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root 'data\logs\server.out.log') -RedirectStandardError (Join-Path $root 'data\logs\server.err.log') -PassThru; " ^
  "  Set-Content -Path (Join-Path $root 'server.pid') -Value \"$($p.Id)\" -Encoding ASCII; " ^
  "  for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Seconds 1; try { $r = Invoke-RestMethod -Uri ($url + '/v1/health') -TimeoutSec 2; if ($r.status) { $alreadyUp = $true; break } } catch {} }; " ^
  "}; " ^
  "$rawArgs = if ($env:FVS_BAT_ARGS) { $env:FVS_BAT_ARGS } else { '' }; " ^
  "if ($alreadyUp) { Write-Host ('[filament-vision-server] Listening at ' + $url + '/'); if ($rawArgs -notmatch '(^|\s)--no-browser(\s|$)') { Start-Process ($url + '/') } } else { Write-Error ('Server failed to respond at ' + $url + '/v1/health'); exit 1 }"
if errorlevel 1 (
    pause
    exit /b 1
)
exit /b 0
