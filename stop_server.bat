@echo off
setlocal
set "ROOT=%~dp0"
set "FVS_BAT_ROOT=%ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = $env:FVS_BAT_ROOT.TrimEnd('\'); " ^
  "$pidFile = Join-Path $root 'server.pid'; " ^
  "$stopped = $false; " ^
  "if (Test-Path $pidFile) { " ^
  "  $raw = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1); " ^
  "  if ($raw -match '^\d+$') { $pidNum = [int]$raw; $cim = Get-CimInstance Win32_Process -Filter \"ProcessId = $pidNum\" -ErrorAction SilentlyContinue; if ($cim -and ($cim.CommandLine -match 'filament[_-]vision[_-]server|spool-server')) { Stop-Process -Id $pidNum -Force -ErrorAction SilentlyContinue; $stopped = $true; Write-Host ('[stop_server.bat] Stopped PID ' + $raw) } }; " ^
  "  Remove-Item $pidFile -Force -ErrorAction SilentlyContinue; " ^
  "}; " ^
  "$normRoot = $root.ToLowerInvariant(); " ^
  "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -like '*filament*vision*server*' -and $_.CommandLine -like '*serve*' -and $_.CommandLine.ToLowerInvariant().Contains($normRoot) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $stopped = $true; Write-Host ('[stop_server.bat] Stopped PID ' + $_.ProcessId) }; " ^
  "if (-not $stopped) { Write-Host '[stop_server.bat] No running server process found.' }"
exit /b 0
