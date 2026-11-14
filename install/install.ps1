[CmdletBinding()]
param(
    [ValidateSet("auto", "cpu", "gpu")]
    [string]$Tier = "auto",
    [int]$Port = 7920,
    [string]$Bind = "127.0.0.1",
    [string]$Prefix = "",
    [switch]$WithGpu,
    [switch]$RegisterStartup,
    [switch]$Start,
    [switch]$OpenBrowser,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
if ([string]::IsNullOrWhiteSpace($Prefix)) {
    $TargetPrefix = $ProjectRoot
} else {
    $TargetPrefix = [System.IO.Path]::GetFullPath($Prefix)
}

function Stop-FilamentServerProcess {
    param([string]$RootDir)
    $pidFile = Join-Path $RootDir "server.pid"
    if (Test-Path $pidFile) {
        $rawPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($rawPid -match '^\d+$') {
            $proc = Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "[install.ps1] Stopping server PID $rawPid..."
                Stop-Process -Id ([int]$rawPid) -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
    try {
        $normRoot = $RootDir.TrimEnd('\').ToLowerInvariant()
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.CommandLine -and
            ($_.CommandLine -like "*filament_vision_server*" -or $_.CommandLine -like "*filament-vision-server*") -and
            $_.CommandLine -like "*serve*" -and
            $_.CommandLine.ToLowerInvariant().Contains($normRoot)
        } | ForEach-Object {
            Write-Host "[install.ps1] Stopping matching process PID $($_.ProcessId)..."
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {
        # Best effort cleanup
    }
}

if ($Uninstall) {
    Write-Host "[install.ps1] Uninstalling filament-vision-server from $TargetPrefix..."
    Stop-FilamentServerProcess -RootDir $TargetPrefix

    try {
        $task = Get-ScheduledTask -TaskName "FilamentVisionServer" -ErrorAction SilentlyContinue
        if ($task) {
            Unregister-ScheduledTask -TaskName "FilamentVisionServer" -Confirm:$false -ErrorAction SilentlyContinue
            Write-Host "[install.ps1] Removed Scheduled Task 'FilamentVisionServer'."
        }
    } catch {
        # Ignore if scheduled tasks unavailable
    }

    $venvDir = Join-Path $TargetPrefix ".venv"
    if (Test-Path $venvDir) {
        Remove-Item $venvDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    $marker = Join-Path $TargetPrefix ".installed"
    if (Test-Path $marker) {
        Remove-Item $marker -Force -ErrorAction SilentlyContinue
    }
    if ($TargetPrefix.TrimEnd('\') -ne $ProjectRoot.TrimEnd('\') -and (Test-Path $TargetPrefix)) {
        Remove-Item $TargetPrefix -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[install.ps1] Uninstalled cleanly."
    exit 0
}

function Find-SupportedPython {
    $candidates = @(
        @("py", "-3.12"),
        @("py", "-3.11"),
        @("py", "-3.13"),
        @("py", "-3.14"),
        @("py", "-3.10"),
        @("python"),
        @("python3")
    )
    foreach ($cand in $candidates) {
        $exe = $cand[0]
        $prefixArgs = @()
        if ($cand.Count -gt 1) {
            $prefixArgs = $cand[1..($cand.Count - 1)]
        }
        $cmdInfo = Get-Command $exe -ErrorAction SilentlyContinue
        if (-not $cmdInfo) { continue }
        try {
            $code = "import sys; print(sys.executable if (3,10)<=sys.version_info[:2]<=(3,14) else '')"
            $resolved = & $exe @prefixArgs -c $code 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                $trimmed = ($resolved | Select-Object -First 1).ToString().Trim()
                if ($trimmed -and (Test-Path $trimmed)) {
                    return $trimmed
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

$PythonExe = Find-SupportedPython
if (-not $PythonExe) {
    Write-Error "Python 3.10 - 3.14 was not found on PATH. Please install Python 3.12 (x64) and retry."
    exit 1
}
Write-Host "[install.ps1] Using Python: $PythonExe"

$venvDir = Join-Path $TargetPrefix ".venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"
$venvPip = Join-Path $venvDir "Scripts\pip.exe"
$venvCli = Join-Path $venvDir "Scripts\filament-vision-server.exe"

if (-not (Test-Path $TargetPrefix)) {
    New-Item -ItemType Directory -Path $TargetPrefix -Force | Out-Null
}

if ($TargetPrefix.TrimEnd('\') -ne $ProjectRoot.TrimEnd('\')) {
    Write-Host "[install.ps1] Copying assets into $TargetPrefix..."
    foreach ($rel in @("configs", "data\catalog", "models")) {
        $srcPath = Join-Path $ProjectRoot $rel
        $dstPath = Join-Path $TargetPrefix $rel
        if (Test-Path $srcPath) {
            New-Item -ItemType Directory -Path $dstPath -Force | Out-Null
            Copy-Item -Path (Join-Path $srcPath "*") -Destination $dstPath -Recurse -Force
        }
    }
}

New-Item -ItemType Directory -Path (Join-Path $TargetPrefix "data\logs") -Force | Out-Null

if (-not (Test-Path $venvPy)) {
    Write-Host "[install.ps1] Creating virtual environment at $venvDir..."
    & $PythonExe -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment." }
}

Write-Host "[install.ps1] Upgrading pip & wheel..."
& $venvPy -m pip install --upgrade pip wheel
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }

$useGpuExtras = $WithGpu -or ($Tier -eq "gpu")
$isInPlace = ($TargetPrefix.TrimEnd('\') -eq $ProjectRoot.TrimEnd('\'))
if ($isInPlace) {
    $spec = if ($useGpuExtras) { "$ProjectRoot[gpu]" } else { $ProjectRoot }
    Write-Host "[install.ps1] Installing package in-place (-e $spec)..."
    & $venvPip install --no-cache-dir -e $spec
} else {
    $spec = if ($useGpuExtras) { "$ProjectRoot[gpu]" } else { $ProjectRoot }
    Write-Host "[install.ps1] Installing package ($spec)..."
    & $venvPip install --no-cache-dir $spec
}
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

$env:FILAMENT_VISION_SERVER_HOME = $TargetPrefix
& $venvPy -m filament_vision_server.service.cli config --tier $Tier --host $Bind --port $Port | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to write service_config.xml." }

Set-Content -Path (Join-Path $TargetPrefix ".installed") -Value "1.0.0" -Encoding ASCII

if ($RegisterStartup) {
    try {
        $actionArgs = "serve --host $Bind --port $Port --tier $Tier"
        $action = New-ScheduledTaskAction -Execute $venvCli -Argument $actionArgs -WorkingDirectory $TargetPrefix
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName "FilamentVisionServer" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Write-Host "[install.ps1] Registered logon Scheduled Task 'FilamentVisionServer'."
    } catch {
        Write-Warning "[install.ps1] Could not register Scheduled Task: $_"
    }
}

$healthHost = if ($Bind -eq "0.0.0.0" -or $Bind -eq "::") { "127.0.0.1" } else { $Bind }
$baseUrl = "http://${healthHost}:${Port}"
$healthUrl = "${baseUrl}/v1/health"

if ($Start) {
    Stop-FilamentServerProcess -RootDir $TargetPrefix
    $outLog = Join-Path $TargetPrefix "data\logs\server.out.log"
    $errLog = Join-Path $TargetPrefix "data\logs\server.err.log"
    Write-Host "[install.ps1] Starting background server on ${baseUrl}..."
    $proc = Start-Process -FilePath $venvPy `
        -ArgumentList @("-m", "filament_vision_server.service.cli", "serve", "--host", $Bind, "--port", "$Port", "--tier", $Tier) `
        -WorkingDirectory $TargetPrefix `
        -WindowStyle Hidden `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog `
        -PassThru
    Set-Content -Path (Join-Path $TargetPrefix "server.pid") -Value "$($proc.Id)" -Encoding ASCII

    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $resp = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2 -ErrorAction Stop
            if ($resp.status -eq "ok" -or $resp.status -eq "degraded") {
                $ready = $true
                break
            }
        } catch {
            # Wait for startup
        }
    }
    if (-not $ready) {
        Write-Warning "Server did not respond on $healthUrl within 30 seconds. Check $errLog"
    } else {
        Write-Host "[install.ps1] Server is healthy at $healthUrl"
        if ($OpenBrowser) {
            Start-Process "$baseUrl/"
        }
    }
}

Write-Host "[install.ps1] Installation complete! Web UI: $baseUrl/"
