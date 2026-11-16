[CmdletBinding()]
param(
    [ValidateSet("auto", "cpu", "gpu")]
    [string]$Tier = "auto",
    [int]$Port = 7920,
    [string]$Bind = "127.0.0.1",
    [string]$Prefix = "",
    [string]$StateDir = "",
    [switch]$WithGpu,
    [switch]$RegisterStartup,
    [switch]$Start,
    [switch]$OpenBrowser,
    [switch]$NonInteractive,
    [switch]$Uninstall,
    [switch]$Purge
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
if ([string]::IsNullOrWhiteSpace($Prefix)) {
    $TargetPrefix = $ProjectRoot
} else {
    $TargetPrefix = [System.IO.Path]::GetFullPath($Prefix)
}
if ([string]::IsNullOrWhiteSpace($StateDir)) {
    $stateBase = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $env:USERPROFILE "AppData\Local" }
    $TargetStateDir = Join-Path $stateBase "spoolwarden-vision-server"
} else {
    $TargetStateDir = [System.IO.Path]::GetFullPath($StateDir)
}
$IsInteractive = [Environment]::UserInteractive -and (-not $NonInteractive)

function Stop-FilamentServerProcess {
    param([string]$RootDir)
    $pidFile = Join-Path $RootDir "server.pid"
    if (Test-Path $pidFile) {
        $rawPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($rawPid -match '^\d+$') {
            $pidNum = [int]$rawPid
            $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $pidNum" -ErrorAction SilentlyContinue
            if ($cim -and (($cim.CommandLine -match 'spoolwarden[_-]vision[_-]server|filament[_-]vision[_-]server|spool-server') -or ($cim.Name -match '^spoolwarden-vision-server'))) {
                Write-Host "[install.ps1] Stopping server PID $rawPid..."
                Stop-Process -Id $pidNum -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
    try {
        $normRoot = $RootDir.TrimEnd('\').ToLowerInvariant()
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.CommandLine -and
            ($_.CommandLine -like "*spoolwarden_vision_server*" -or $_.CommandLine -like "*spoolwarden-vision-server*") -and
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
    Write-Host "[install.ps1] Uninstalling spoolwarden-vision-server from $TargetPrefix..."
    Stop-FilamentServerProcess -RootDir $TargetPrefix

    try {
        $task = Get-ScheduledTask -TaskName "SpoolwardenVisionServer" -ErrorAction SilentlyContinue
        if ($task) {
            Unregister-ScheduledTask -TaskName "SpoolwardenVisionServer" -Confirm:$false -ErrorAction SilentlyContinue
            Write-Host "[install.ps1] Removed Scheduled Task 'SpoolwardenVisionServer'."
        }
    } catch {
        # Ignore if scheduled tasks unavailable
    }

    $venvDir = Join-Path $TargetPrefix ".venv"
    if (Test-Path $venvDir) {
        Remove-Item $venvDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    foreach ($relFile in @(".installed", "server.pid", "start_server.bat", "stop_server.bat")) {
        $fp = Join-Path $TargetPrefix $relFile
        if (Test-Path $fp) {
            Remove-Item $fp -Force -ErrorAction SilentlyContinue
        }
    }
    if ($TargetPrefix.TrimEnd('\') -ne $ProjectRoot.TrimEnd('\') -and (Test-Path $TargetPrefix)) {
        # Remove staged assets while preserving user data\feedback_queue
        foreach ($relDir in @("configs", "models", "data\catalog", "data\logs")) {
            $dp = Join-Path $TargetPrefix $relDir
            if (Test-Path $dp) {
                Remove-Item $dp -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        $dataDir = Join-Path $TargetPrefix "data"
        if ((Test-Path $dataDir) -and -not (Get-ChildItem -Path $dataDir -Force | Select-Object -First 1)) {
            Remove-Item $dataDir -Force -ErrorAction SilentlyContinue
        }
        if (-not (Get-ChildItem -Path $TargetPrefix -Force | Select-Object -First 1)) {
            Remove-Item $TargetPrefix -Force -ErrorAction SilentlyContinue
        }
    }
    $waitingCount = 0
    foreach ($qxml in @(
        (Join-Path $TargetStateDir "feedback_queue\pending_feedback.xml"),
        (Join-Path $TargetPrefix "data\feedback_queue\pending_feedback.xml")
    )) {
        if (Test-Path $qxml) {
            $waitingCount += ([regex]::Matches((Get-Content $qxml -Raw), "<entry\b")).Count
        }
    }
    if ($Purge) {
        if (Test-Path $TargetStateDir) {
            Remove-Item $TargetStateDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        $legacyFb = Join-Path $TargetPrefix "data\feedback_queue"
        if (Test-Path $legacyFb) {
            Remove-Item $legacyFb -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Host "[install.ps1] Purged state folder $TargetStateDir ($waitingCount waiting correction(s) removed)."
    } else {
        Write-Host "[install.ps1] Preserved state folder $TargetStateDir (config, token, keys, and $waitingCount waiting correction(s)). Re-run with -Uninstall -Purge to remove it."
    }
    Write-Host "[install.ps1] Uninstalled cleanly."
    exit 0
}

function Prompt-ManualDependency {
    param(
        [string]$Name,
        [string]$Reason,
        [string]$Url,
        [string]$Instructions = ""
    )
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host "[Dependency Required] $Name" -ForegroundColor Yellow
    Write-Host "  Reason: $Reason"
    if ($Instructions) {
        Write-Host "  Notes : $Instructions"
    }
    Write-Host "  URL   : $Url"
    Write-Host "============================================================" -ForegroundColor Yellow

    if (-not $IsInteractive) {
        return $false
    }

    $openAns = Read-Host "Would you like to open the download page ($Url) in your browser now? [Y/n]"
    if ([string]::IsNullOrWhiteSpace($openAns) -or $openAns -match '^[Yy]') {
        try {
            Start-Process $Url
        } catch {
            Write-Warning "Could not launch browser automatically. Please open: $Url"
        }
    }
    $waitAns = Read-Host "Complete installation of $Name, then press [Enter] to re-check or type 'Q' to quit"
    if ($waitAns -match '^[Qq]') {
        return $false
    }
    return $true
}

function Update-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $combined = @()
    foreach ($part in ($machinePath + ";" + $userPath) -split ';') {
        if ($part -and ($combined -notcontains $part)) {
            $combined += $part
        }
    }
    $env:Path = $combined -join ';'
}

function Test-VcRedistPresent {
    $sys32 = Join-Path $env:SystemRoot "System32"
    return (Test-Path (Join-Path $sys32 "vcruntime140_1.dll")) -and (Test-Path (Join-Path $sys32 "msvcp140.dll"))
}

function Ensure-VcRedist {
    $vcUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    while (-not (Test-VcRedistPresent)) {
        Write-Host "[install.ps1] Missing Microsoft Visual C++ 2015-2022 Redistributable (x64); attempting automatic install..."

        $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
        if ($wingetCmd) {
            try {
                & winget install --id Microsoft.VCRedist.2015+.x64 -e --accept-package-agreements --accept-source-agreements --silent
                if (Test-VcRedistPresent) {
                    Write-Host "[install.ps1] Installed VC++ Redistributable via winget."
                    return
                }
            } catch {
                Write-Warning "winget VC++ install attempt failed: $_"
            }
        }

        $vcExe = Join-Path $env:TEMP "vc_redist.x64.exe"
        try {
            if ($vcUrl -notmatch '^https://') {
                throw "Refusing non-HTTPS VC++ download URL: $vcUrl"
            }
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Write-Host "[install.ps1] Downloading $vcUrl..."
            Invoke-WebRequest -Uri $vcUrl -OutFile $vcExe -UseBasicParsing
            Write-Host "[install.ps1] Running $vcExe..."
            $vcProc = Start-Process -FilePath $vcExe -ArgumentList "/install", "/passive", "/norestart" -Wait -PassThru
            if ($vcProc.ExitCode -ne 0 -and $vcProc.ExitCode -ne 3010 -and $vcProc.ExitCode -ne 1638) {
                Write-Warning "vc_redist.x64.exe exited with code $($vcProc.ExitCode)."
            }
        } catch {
            Write-Warning "Automatic VC++ Redistributable download/install failed: $_"
        } finally {
            Remove-Item $vcExe -Force -ErrorAction SilentlyContinue
        }

        if (Test-VcRedistPresent) {
            Write-Host "[install.ps1] Microsoft Visual C++ Redistributable (x64) is ready."
            return
        }

        $retry = Prompt-ManualDependency `
            -Name "Microsoft Visual C++ 2015-2022 Redistributable (x64)" `
            -Reason "Required by ONNX Runtime, OpenCV, and pillow-heif native DLLs." `
            -Url $vcUrl `
            -Instructions "Download and run vc_redist.x64.exe as Administrator if prompted."
        if (-not $retry) {
            throw "Microsoft Visual C++ 2015-2022 Redistributable (x64) is required. Download from: $vcUrl"
        }
    }
}

function Test-PythonCandidate {
    param(
        [string]$Exe,
        [string[]]$PrefixArgs = @()
    )
    try {
        $code = "import sys, venv, ensurepip; print(sys.executable if (3,10)<=sys.version_info[:2]<=(3,14) else '')"
        $resolved = & $Exe @PrefixArgs -c $code 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved) {
            $trimmed = ($resolved | Select-Object -First 1).ToString().Trim()
            if ($trimmed -and (Test-Path $trimmed)) {
                return $trimmed
            }
        }
    } catch {
        return $null
    }
    return $null
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
        $hit = Test-PythonCandidate -Exe $exe -PrefixArgs $prefixArgs
        if ($hit) { return $hit }
    }

    $globPatterns = @(
        (Join-Path $env:LocalAppData "Programs\Python\Python31*\python.exe"),
        (Join-Path $env:ProgramFiles "Python31*\python.exe"),
        "C:\Python31*\python.exe",
        (Join-Path $env:AppData "uv\python\cpython-3.1*\python.exe"),
        (Join-Path $env:LocalAppData "uv\python\cpython-3.1*\python.exe")
    )
    foreach ($pat in $globPatterns) {
        $matches = Get-Item -Path $pat -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($m in $matches) {
            $hit = Test-PythonCandidate -Exe $m.FullName
            if ($hit) { return $hit }
        }
    }
    return $null
}

function Ensure-PythonRuntime {
    $pyUrl = "https://www.python.org/downloads/windows/"
    $pyInstallerUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
    while ($true) {
        $found = Find-SupportedPython
        if ($found) { return $found }

        Write-Host "[install.ps1] Python 3.10 - 3.14 not found; attempting automatic installation..."

        # 1. Try winget (user scope, no Admin required)
        $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
        if ($wingetCmd) {
            try {
                Write-Host "[install.ps1] Trying winget install Python.Python.3.12..."
                & winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
                Update-ProcessPath
                $found = Find-SupportedPython
                if ($found) { return $found }
            } catch {
                Write-Warning "winget Python install failed: $_"
            }
        }

        # 2. Try uv standalone Python installer (no Admin required)
        try {
            $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
            $uvExe = if ($uvCmd) { $uvCmd.Source } else { Join-Path $env:UserProfile ".local\bin\uv.exe" }
            if (-not (Test-Path $uvExe)) {
                Write-Host "[install.ps1] Bootstrapping 'uv' to install standalone Python 3.12..."
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-RestMethod "https://astral.sh/uv/install.ps1" | Invoke-Expression
                Update-ProcessPath
                $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
                if ($uvCmd) { $uvExe = $uvCmd.Source }
            }
            if (Test-Path $uvExe) {
                & $uvExe python install 3.12
                $uvFound = (& $uvExe python find 3.12 2>$null | Select-Object -First 1)
                if ($uvFound -and (Test-Path $uvFound.Trim())) {
                    $hit = Test-PythonCandidate -Exe $uvFound.Trim()
                    if ($hit) { return $hit }
                }
                Update-ProcessPath
                $found = Find-SupportedPython
                if ($found) { return $found }
            }
        } catch {
            Write-Warning "uv Python bootstrap failed: $_"
        }

        # 3. Direct download from python.org
        $pySetup = Join-Path $env:TEMP "python-3.12.8-amd64.exe"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Write-Host "[install.ps1] Downloading $pyInstallerUrl..."
            Invoke-WebRequest -Uri $pyInstallerUrl -OutFile $pySetup -UseBasicParsing
            Write-Host "[install.ps1] Installing Python 3.12.8 quietly for current user..."
            Start-Process -FilePath $pySetup -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_test=0", "Include_pip=1", "Include_launcher=1" -Wait
            Update-ProcessPath
            $found = Find-SupportedPython
            if ($found) { return $found }
        } catch {
            Write-Warning "Direct Python 3.12 download/install failed: $_"
        } finally {
            Remove-Item $pySetup -Force -ErrorAction SilentlyContinue
        }

        $retry = Prompt-ManualDependency `
            -Name "Python 3.12 (x64)" `
            -Reason "Python 3.10 - 3.14 could not be downloaded/installed automatically." `
            -Url $pyUrl `
            -Instructions "Download Windows installer (64-bit) and check 'Add python.exe to PATH' during setup."
        if (-not $retry) {
            throw "Python 3.10 - 3.14 is required. Please install from $pyUrl and retry."
        }
        Update-ProcessPath
    }
}

function Test-NvidiaGpuReady {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) { return $false }
    try {
        & nvidia-smi *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Ensure-GpuDependencies {
    $cudaUrl = "https://www.nvidia.com/Download/index.aspx"
    while ($WithGpu -or ($Tier -eq "gpu")) {
        if (Test-NvidiaGpuReady) {
            Write-Host "[install.ps1] NVIDIA CUDA GPU detected via nvidia-smi."
            return $true
        }
        Write-Warning "GPU tier was requested (-Tier $Tier / -WithGpu), but 'nvidia-smi' did not detect an active NVIDIA driver."
        if (-not $IsInteractive) {
            throw "NVIDIA GPU / driver not detected. Install NVIDIA drivers from $cudaUrl or run with -Tier cpu."
        }
        Write-Host "Options:"
        Write-Host "  [1] Continue installation on CPU / auto tier instead"
        Write-Host "  [2] Open NVIDIA Driver Downloads ($cudaUrl) and re-check"
        Write-Host "  [3] Quit"
        $sel = Read-Host "Selection [1]"
        if ([string]::IsNullOrWhiteSpace($sel) -or $sel -eq "1") {
            $script:Tier = "auto"
            $script:WithGpu = $false
            Write-Host "[install.ps1] Continuing with Tier=auto (CPU ONNX pipeline)."
            return $false
        } elseif ($sel -eq "2") {
            try { Start-Process $cudaUrl } catch {}
            Read-Host "Press [Enter] after installing NVIDIA drivers to re-check" | Out-Null
        } else {
            exit 1
        }
    }
    return (Test-NvidiaGpuReady)
}

function Test-ModelsPresent {
    param([string]$ModelsRoot)
    $required = @(
        "detector_rfdetr_n.onnx",
        "visual_multihead.onnx",
        "text_parser.npz",
        "text_parser_meta.xml",
        "visual_meta.xml"
    )
    foreach ($f in $required) {
        $p1 = Join-Path $ModelsRoot "cpu\$f"
        $p2 = Join-Path $ModelsRoot $f
        if (-not (Test-Path $p1) -and -not (Test-Path $p2)) {
            return $false
        }
    }
    return $true
}

function Ensure-ModelAssets {
    param([string]$DefaultModelsDir)
    while ($true) {
        if (Test-ModelsPresent -ModelsRoot $DefaultModelsDir) {
            return $DefaultModelsDir
        }

        if ($env:SPOOLWARDEN_VISION_SERVER_ASSETS_URL) {
            $assetsUrl = $env:SPOOLWARDEN_VISION_SERVER_ASSETS_URL.Trim()
            if ($assetsUrl -notmatch '^https://' -and $assetsUrl -notmatch '^http://(127\.0\.0\.1|localhost)(:\d+)?/') {
                throw "Refusing insecure non-HTTPS SPOOLWARDEN_VISION_SERVER_ASSETS_URL: $assetsUrl"
            }
            $zipTmp = Join-Path $env:TEMP "spoolwarden_vision_models.zip"
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Write-Host "[install.ps1] Downloading model assets from ${assetsUrl}..."
                Invoke-WebRequest -Uri $assetsUrl -OutFile $zipTmp -UseBasicParsing
                New-Item -ItemType Directory -Path $DefaultModelsDir -Force | Out-Null
                Expand-Archive -Path $zipTmp -DestinationPath $DefaultModelsDir -Force
                $nested = Join-Path $DefaultModelsDir "models"
                if ((Test-Path $nested) -and (Test-ModelsPresent -ModelsRoot $nested)) {
                    Copy-Item -Path (Join-Path $nested "*") -Destination $DefaultModelsDir -Recurse -Force
                }
                if (Test-ModelsPresent -ModelsRoot $DefaultModelsDir) { return $DefaultModelsDir }
            } catch {
                Write-Warning "Model archive download failed: $_"
            } finally {
                Remove-Item $zipTmp -Force -ErrorAction SilentlyContinue
            }
        }

        if (-not $IsInteractive) {
            throw "Required CPU model files missing under $DefaultModelsDir. Ensure bundled models/ exists or set SPOOLWARDEN_VISION_SERVER_ASSETS_URL."
        }

        Write-Host ""
        Write-Host "[install.ps1] Model weights not found under: $DefaultModelsDir" -ForegroundColor Yellow
        $ans = Read-Host "Enter a local models folder path or HTTPS .zip download URL (or type 'Q' to quit)"
        if ([string]::IsNullOrWhiteSpace($ans) -or $ans -match '^[Qq]$') {
            exit 1
        }
        if ($ans -match '^https://' -or $ans -match '^http://(127\.0\.0\.1|localhost)(:\d+)?/') {
            $zipTmp = Join-Path $env:TEMP "spoolwarden_vision_models.zip"
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -Uri $ans -OutFile $zipTmp -UseBasicParsing
                New-Item -ItemType Directory -Path $DefaultModelsDir -Force | Out-Null
                Expand-Archive -Path $zipTmp -DestinationPath $DefaultModelsDir -Force
                $nested = Join-Path $DefaultModelsDir "models"
                if ((Test-Path $nested) -and (Test-ModelsPresent -ModelsRoot $nested)) {
                    Copy-Item -Path (Join-Path $nested "*") -Destination $DefaultModelsDir -Recurse -Force
                }
            } catch {
                Write-Warning "Failed to download or extract models from ${ans}: $_"
            } finally {
                Remove-Item $zipTmp -Force -ErrorAction SilentlyContinue
            }
        } elseif ($ans -match '^http://') {
            Write-Warning "Plain HTTP download URLs are rejected for remote assets. Use HTTPS or a local folder path."
        } elseif ((Test-Path $ans) -and (Test-ModelsPresent -ModelsRoot $ans)) {
            return (Resolve-Path $ans).Path
        } else {
            Write-Warning "Required model files were not found in: $ans"
        }
    }
}

# Step 1: Check native runtime DLLs & Python 3.10-3.14
Ensure-VcRedist
$PythonExe = Ensure-PythonRuntime
Write-Host "[install.ps1] Using Python: $PythonExe"

# Step 2: Check GPU driver if requested
$CudaReady = Ensure-GpuDependencies

# Step 3: Ensure model weights exist before staging target prefix
$SourceModelsDir = Ensure-ModelAssets -DefaultModelsDir (Join-Path $ProjectRoot "models")

$venvDir = Join-Path $TargetPrefix ".venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"
$venvPip = Join-Path $venvDir "Scripts\pip.exe"
$venvCli = Join-Path $venvDir "Scripts\spoolwarden-vision-server.exe"

if (-not (Test-Path $TargetPrefix)) {
    New-Item -ItemType Directory -Path $TargetPrefix -Force | Out-Null
}

function Copy-FilamentModelAssets {
    param([string]$SrcModelsDir, [string]$DstModelsDir)
    $dstCpuModels = Join-Path $DstModelsDir "cpu"
    New-Item -ItemType Directory -Path $dstCpuModels -Force | Out-Null
    $srcCpuModels = Join-Path $SrcModelsDir "cpu"
    if (Test-Path $srcCpuModels) {
        Get-ChildItem -Path $srcCpuModels -File | Where-Object { $_.Extension -notin @('.pt', '.pth', '.ckpt') } | ForEach-Object {
            Copy-Item -Path $_.FullName -Destination (Join-Path $dstCpuModels $_.Name) -Force
        }
    }
    Get-ChildItem -Path $SrcModelsDir -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in @('.onnx', '.npz', '.xml', '.json') } | ForEach-Object {
        Copy-Item -Path $_.FullName -Destination (Join-Path $dstCpuModels $_.Name) -Force
    }
    $srcGpuModels = Join-Path $SrcModelsDir "gpu"
    if (Test-Path $srcGpuModels) {
        $dstGpuModels = Join-Path $DstModelsDir "gpu"
        New-Item -ItemType Directory -Path $dstGpuModels -Force | Out-Null
        Copy-Item -Path (Join-Path $srcGpuModels "*") -Destination $dstGpuModels -Recurse -Force
    }
}

if ($TargetPrefix.TrimEnd('\') -ne $ProjectRoot.TrimEnd('\')) {
    Write-Host "[install.ps1] Copying assets into $TargetPrefix..."
    $dstConfigs = Join-Path $TargetPrefix "configs"
    New-Item -ItemType Directory -Path $dstConfigs -Force | Out-Null
    foreach ($cfgName in @("taxonomy.xml", "cpu_pipeline.xml", "gpu_vlm_lora.xml", "dataset_manifest.xml", "base-model.json")) {
        $srcCfg = Join-Path (Join-Path $ProjectRoot "configs") $cfgName
        if (Test-Path $srcCfg) {
            Copy-Item -Path $srcCfg -Destination (Join-Path $dstConfigs $cfgName) -Force
        }
    }
    $srcCatalog = Join-Path $ProjectRoot "data\catalog"
    $dstCatalog = Join-Path $TargetPrefix "data\catalog"
    if (Test-Path $srcCatalog) {
        New-Item -ItemType Directory -Path $dstCatalog -Force | Out-Null
        Copy-Item -Path (Join-Path $srcCatalog "*") -Destination $dstCatalog -Recurse -Force
    }
    foreach ($batFile in @("start_server.bat", "stop_server.bat")) {
        $srcBat = Join-Path $ProjectRoot $batFile
        if (Test-Path $srcBat) {
            Copy-Item -Path $srcBat -Destination (Join-Path $TargetPrefix $batFile) -Force
        }
    }
    Copy-FilamentModelAssets -SrcModelsDir $SourceModelsDir -DstModelsDir (Join-Path $TargetPrefix "models")
} elseif ($SourceModelsDir.TrimEnd('\') -ne (Join-Path $ProjectRoot "models").TrimEnd('\')) {
    Copy-FilamentModelAssets -SrcModelsDir $SourceModelsDir -DstModelsDir (Join-Path $ProjectRoot "models")
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

if ($useGpuExtras -and $CudaReady) {
    Write-Host "[install.ps1] Installing CUDA-enabled PyTorch wheels (cu124)..."
    & $venvPip install --no-cache-dir torch --extra-index-url https://download.pytorch.org/whl/cu124
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "CUDA PyTorch index install failed; continuing with standard PyPI package resolution."
    }
}

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

if ($useGpuExtras -and $CudaReady) {
    Write-Host "[install.ps1] CUDA GPU extras enabled; switching onnxruntime to onnxruntime-gpu..."
    & $venvPip uninstall -y onnxruntime 2>$null
    & $venvPip install --no-cache-dir --upgrade onnxruntime-gpu 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "onnxruntime-gpu install failed; restoring CPU onnxruntime..."
        & $venvPip install --no-cache-dir onnxruntime
    }
}

function Test-AndRepairPythonDependencies {
    param([bool]$WantGpu, [bool]$UseCuda = $false)
    $gpuFlag = if ($WantGpu) { "1" } else { "0" }
    $cudaFlag = if ($UseCuda) { "1" } else { "0" }
    $probeScript = @"
import importlib, sys
want_gpu = sys.argv[1] == '1'
use_cuda = sys.argv[2] == '1'
checks = [
    ('defusedxml', 'defusedxml'),
    ('PIL', 'Pillow'),
    ('pillow_heif', 'pillow-heif'),
    ('numpy', 'numpy'),
    ('cv2', 'opencv-python-headless'),
    ('onnxruntime', 'onnxruntime-gpu' if (want_gpu and use_cuda) else 'onnxruntime'),
    ('rapidfuzz', 'rapidfuzz'),
    ('rapidocr', 'rapidocr'),
    ('sklearn', 'scikit-learn'),
    ('cryptography', 'cryptography'),
]
if want_gpu:
    checks.extend([
        ('torch', 'torch'),
        ('transformers', 'transformers'),
        ('peft', 'peft'),
        ('accelerate', 'accelerate'),
        ('bitsandbytes', 'bitsandbytes'),
        ('qwen_vl_utils', 'qwen-vl-utils'),
    ])
missing = []
for mod, pkg in checks:
    try:
        importlib.import_module(mod)
    except Exception:
        missing.append(pkg)
print(' '.join(dict.fromkeys(missing)))
"@
    while ($true) {
        $missingOut = (& $venvPy -c $probeScript $gpuFlag $cudaFlag | Select-Object -First 1)
        $missingList = if ($missingOut) { $missingOut.Trim() } else { "" }
        if ([string]::IsNullOrWhiteSpace($missingList)) {
            Write-Host "[install.ps1] Verified all Python dependencies import cleanly."
            return
        }
        $pkgs = $missingList -split '\s+'
        Write-Host "[install.ps1] Attempting auto-repair for failed Python packages: $missingList"
        if ($UseCuda -and ($pkgs -contains 'torch')) {
            & $venvPip install --no-cache-dir --force-reinstall @pkgs --extra-index-url https://download.pytorch.org/whl/cu124
        } else {
            & $venvPip install --no-cache-dir --force-reinstall @pkgs
        }

        $stillOut = (& $venvPy -c $probeScript $gpuFlag $cudaFlag | Select-Object -First 1)
        $stillList = if ($stillOut) { $stillOut.Trim() } else { "" }
        if ([string]::IsNullOrWhiteSpace($stillList)) {
            Write-Host "[install.ps1] Auto-repair succeeded; all Python dependencies verified."
            return
        }
        $retry = Prompt-ManualDependency `
            -Name "Python runtime packages ($stillList)" `
            -Reason "Package import check still fails inside $venvDir." `
            -Url "https://pypi.org/" `
            -Instructions "Ensure internet access and retry: $venvPip install --no-cache-dir $stillList"
        if (-not $retry) {
            throw "Required Python packages failed verification: $stillList"
        }
    }
}

Test-AndRepairPythonDependencies -WantGpu:$useGpuExtras -UseCuda:$CudaReady

New-Item -ItemType Directory -Force -Path $TargetStateDir | Out-Null
$upgradeDetected = (Test-Path (Join-Path $TargetStateDir "service_config.xml")) `
    -or (Test-Path (Join-Path $TargetPrefix ".installed")) `
    -or (Test-Path (Join-Path $TargetPrefix "configs\service_config.xml")) `
    -or (Test-Path (Join-Path $TargetPrefix "data\feedback_queue"))
$legacyCfg = Join-Path $TargetPrefix "configs\service_config.xml"
$stateCfg = Join-Path $TargetStateDir "service_config.xml"
if ((Test-Path $legacyCfg) -and -not (Test-Path $stateCfg)) {
    Copy-Item -Path $legacyCfg -Destination $stateCfg -Force
}
$legacyQueue = Join-Path $TargetPrefix "data\feedback_queue"
$stateQueue = Join-Path $TargetStateDir "feedback_queue"
if ((Test-Path $legacyQueue) -and -not (Test-Path $stateQueue)) {
    Copy-Item -Path $legacyQueue -Destination $stateQueue -Recurse -Force
}
if ($upgradeDetected) {
    Write-Host "[install.ps1] Upgrading in place: preserved existing config, token, keys, and feedback queue in $TargetStateDir."
}

$savedEnvVars = @{}
foreach ($envName in @("SPOOLWARDEN_VISION_SERVER_TOKEN", "FILAMENT_KIT_VISION_TOKEN", "SPOOLWARDEN_VISION_SERVER_FEEDBACK_ENDPOINT", "FILAMENT_KIT_VISION_FEEDBACK_ENDPOINT")) {
    $savedEnvVars[$envName] = [Environment]::GetEnvironmentVariable($envName, "Process")
    [Environment]::SetEnvironmentVariable($envName, $null, "Process")
}
try {
    $env:SPOOLWARDEN_VISION_SERVER_HOME = $TargetPrefix
    $env:SPOOLWARDEN_VISION_SERVER_STATE_DIR = $TargetStateDir
    & $venvPy -m spoolwarden_vision_server.service.cli config --tier $Tier --host $Bind --port $Port | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to write service_config.xml." }
} finally {
    foreach ($envName in $savedEnvVars.Keys) {
        [Environment]::SetEnvironmentVariable($envName, $savedEnvVars[$envName], "Process")
    }
}

Set-Content -Path (Join-Path $TargetPrefix ".installed") -Value "0.3.1" -Encoding ASCII

if ($RegisterStartup) {
    try {
        $taskCmdArgs = "/c set `"SPOOLWARDEN_VISION_SERVER_HOME=$TargetPrefix`" && set `"SPOOLWARDEN_VISION_SERVER_STATE_DIR=$TargetStateDir`" && `"$venvCli`" serve"
        $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $taskCmdArgs -WorkingDirectory $TargetPrefix
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName "SpoolwardenVisionServer" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Write-Host "[install.ps1] Registered logon Scheduled Task 'SpoolwardenVisionServer'."
    } catch {
        Write-Warning "[install.ps1] Could not register Scheduled Task: $_"
    }
}

$healthHost = if ($Bind -eq "0.0.0.0" -or $Bind -eq "::") { "127.0.0.1" } elseif ($Bind.Contains(":")) { "[$Bind]" } else { $Bind }
$baseUrl = "http://${healthHost}:${Port}"
$healthUrl = "${baseUrl}/v1/health"

if ($Start) {
    Stop-FilamentServerProcess -RootDir $TargetPrefix
    $outLog = Join-Path $TargetPrefix "data\logs\server.out.log"
    $errLog = Join-Path $TargetPrefix "data\logs\server.err.log"
    Write-Host "[install.ps1] Starting background server on ${baseUrl}..."
    $proc = Start-Process -FilePath $venvPy `
        -ArgumentList @("-m", "spoolwarden_vision_server.service.cli", "serve") `
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
