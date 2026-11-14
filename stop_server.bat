@echo off
setlocal
set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = '%ROOT%'.TrimEnd('\'); " ^
  "$pidFile = Join-Path $root 'server.pid'; " ^
  "$stopped = $false; " ^
  "if (Test-Path $pidFile) { " ^
  "  $raw = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1); " ^
  "  if ($raw -match '^\d+$') { $proc = Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue; if ($proc) { Stop-Process -Id ([int]$raw) -Force -ErrorAction SilentlyContinue; $stopped = $true; Write-Host ('[stop_server.bat] Stopped PID ' + $raw) } }; " ^
  "  Remove-Item $pidFile -Force -ErrorAction SilentlyContinue; " ^
  "}; " ^
  "$normRoot = $root.ToLowerInvariant(); " ^
  "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -like '*filament*vision*server*' -and $_.CommandLine -like '*serve*' -and $_.CommandLine.ToLowerInvariant().Contains($normRoot) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $stopped = $true; Write-Host ('[stop_server.bat] Stopped PID ' + $_.ProcessId) }; " ^
  "if (-not $stopped) { Write-Host '[stop_server.bat] No running server process found.' }"
exit /b 0
