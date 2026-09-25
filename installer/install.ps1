# Lorekeeper installer (per-user, no admin needed).
# Installs: app files, Python 3.12 (if missing), a private virtualenv with CUDA PyTorch,
# Ollama (if missing), an AI model sized to the GPU, the embedding model, shortcuts, and an
# Add/Remove Programs entry. Safe to re-run: it upgrades in place and keeps your library.
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Lorekeeper",
    [string]$ImportData = "",
    [switch]$NoLaunch
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Host.UI.RawUI.WindowTitle = "Lorekeeper Setup"
$Version = "1.0.0"
$Steps = 8

function Step($n, $msg) { Write-Host ""; Write-Host "[$n/$Steps] $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "      $msg" -ForegroundColor Green }
function Info($msg) { Write-Host "      $msg" -ForegroundColor DarkGray }
function Fail($msg) {
    Write-Host ""
    Write-Host "Setup failed: $msg" -ForegroundColor Red
    Write-Host "Fix the problem above and run the installer again - it continues where it stopped." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}
function Run([string]$exe, [string[]]$argList, [string]$what) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { Fail "$what (exit code $LASTEXITCODE)" }
}
function Download([string]$url, [string]$dest) {
    Info "Downloading $url"
    try { Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing }
    catch { Fail "Download failed: $url - $($_.Exception.Message)" }
}

Write-Host ""
Write-Host "  Lorekeeper $Version - setup" -ForegroundColor White
Write-Host "  Installing to $InstallDir" -ForegroundColor DarkGray
Write-Host "  This downloads Python packages, PyTorch, Ollama and an AI model (up to ~9 GB)." -ForegroundColor DarkGray

# ------------------------------------------------------------------ 1. app files
Step 1 "Copying Lorekeeper"
function Test-Library([string]$dir) {
    # "smart_reader.db" is the database name used before the app was renamed to Lorekeeper.
    return $dir -and ((Test-Path (Join-Path $dir "lorekeeper.db")) -or (Test-Path (Join-Path $dir "smart_reader.db")))
}
$here = $PSScriptRoot
if (Test-Path (Join-Path $here "app.zip")) {
    $src = Join-Path $env:TEMP "LorekeeperSetup_src"
    Remove-Item -Recurse -Force $src -ErrorAction SilentlyContinue
    Expand-Archive -Path (Join-Path $here "app.zip") -DestinationPath $src -Force
} elseif (Test-Path (Join-Path $here "..\launcher.py")) {
    $src = (Resolve-Path (Join-Path $here "..")).Path
    if (-not $ImportData -and (Test-Library (Join-Path $src "data"))) { $ImportData = Join-Path $src "data" }
} else {
    Fail "Cannot find the Lorekeeper files next to the installer."
}

# Upgrade from the earlier "Smart Reader" install: move it (library and environment included)
# so nothing has to be downloaded again, and remove its old shortcuts and registration.
$legacyDir = Join-Path $env:LOCALAPPDATA "SmartReader"
if ((Test-Path $legacyDir) -and ($legacyDir -ne $InstallDir)) {
    Info "Upgrading the previous Smart Reader installation"
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$legacyDir*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    if (-not (Test-Path $InstallDir)) {
        Move-Item $legacyDir $InstallDir -ErrorAction SilentlyContinue
    }
    if (Test-Path $legacyDir) {
        if (-not $ImportData -and (Test-Library (Join-Path $legacyDir "data"))) { $ImportData = Join-Path $legacyDir "data" }
    }
    Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) "Smart Reader.lnk") -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Smart Reader") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SmartReader" -Recurse -Force -ErrorAction SilentlyContinue
}

# Close a running copy so files can be replaced.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$InstallDir*launcher.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if ((Resolve-Path $src).Path.TrimEnd('\') -ne (Resolve-Path $InstallDir).Path.TrimEnd('\')) {
    robocopy $src $InstallDir /E /XD data venv __pycache__ .git samples dist tests docs .pytest_cache /XF *.pyc run.ps1 .gitignore "Install Lorekeeper.cmd" /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "Copying files failed (robocopy $LASTEXITCODE)" }
}
Copy-Item (Join-Path $InstallDir "installer\uninstall.ps1") (Join-Path $InstallDir "uninstall.ps1") -Force
Ok "Files installed"

# ------------------------------------------------------------------ 2. Python
Step 2 "Checking for Python 3.10 - 3.13"
function Find-Python {
    $cands = New-Object System.Collections.Generic.List[string]
    foreach ($v in "312", "311", "313", "310") {
        foreach ($base in "$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles") {
            $p = Join-Path $base "Python$v\python.exe"
            if (Test-Path $p) { $cands.Add($p) }
        }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in "3.12", "3.11", "3.13", "3.10") {
            $p = (& py "-$v" -c "import sys;print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $p) { $cands.Add($p.Trim()) }
        }
    }
    $pc = Get-Command python -ErrorAction SilentlyContinue
    if ($pc -and $pc.Source -notlike "*WindowsApps*") { $cands.Add($pc.Source) }
    foreach ($c in $cands) {
        $v = (& $c -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
        if ($v -in @("3.10", "3.11", "3.12", "3.13")) { return $c }
    }
    return $null
}
$venvPy = Join-Path $InstallDir "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $py = Find-Python
    if (-not $py) {
        Info "Python not found - installing Python 3.12 for this user"
        $pyInst = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
        Download "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" $pyInst
        $p = Start-Process $pyInst -ArgumentList "/quiet InstallAllUsers=0 PrependPath=0 Include_test=0 Include_launcher=0 Shortcuts=0" -Wait -PassThru
        if ($p.ExitCode -ne 0) { Fail "Python installer returned $($p.ExitCode)" }
        $py = Find-Python
        if (-not $py) { Fail "Python was installed but could not be found." }
    }
    Ok "Using $py"
} else {
    Ok "Existing environment found"
}

# ------------------------------------------------------------------ 3. virtualenv
Step 3 "Creating the app environment"
if (-not (Test-Path $venvPy)) {
    Run $py @("-m", "venv", (Join-Path $InstallDir "venv")) "Creating virtual environment"
}
Run $venvPy @("-m", "pip", "install", "--upgrade", "pip", "--quiet", "--disable-pip-version-check") "Upgrading pip"
Ok "Environment ready"

# ------------------------------------------------------------------ 4. PyTorch (GPU)
Step 4 "Installing PyTorch"
$gpu = $null; $vramMB = 0
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $line = (& nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
    if ($line) {
        $parts = $line.Split(",") | ForEach-Object { $_.Trim() }
        $gpu = $parts[0]; $vramMB = [int]$parts[1]; $driver = [double]($parts[2].Split(".")[0])
    }
}
if ($gpu) {
    $cuda = if ($driver -ge 570) { "cu128" } else { "cu126" }
    Info "GPU: $gpu ($vramMB MB) - using CUDA build ($cuda)"
    & $venvPy -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Run $venvPy @("-m", "pip", "install", "torch", "--index-url", "https://download.pytorch.org/whl/$cuda", "--disable-pip-version-check") "Installing PyTorch (CUDA)"
    }
} else {
    Info "No NVIDIA GPU found - using the CPU build (analysis will be slow; consider Groq in Settings)"
    & $venvPy -c "import torch" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Run $venvPy @("-m", "pip", "install", "torch", "--index-url", "https://download.pytorch.org/whl/cpu", "--disable-pip-version-check") "Installing PyTorch (CPU)"
    }
}
Ok "PyTorch ready"

# ------------------------------------------------------------------ 5. Python packages
Step 5 "Installing Lorekeeper packages"
Run $venvPy @("-m", "pip", "install", "-r", (Join-Path $InstallDir "requirements.txt"), "--disable-pip-version-check") "Installing packages"
Ok "Packages ready"

# ------------------------------------------------------------------ 6. Ollama
Step 6 "Setting up Ollama (runs AI models on your GPU)"
function Find-Ollama {
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe", "$env:ProgramFiles\Ollama\ollama.exe") {
        if (Test-Path $p) { return $p }
    }
    return $null
}
function Test-Ollama {
    try { return (Invoke-WebRequest "http://127.0.0.1:11434/api/version" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
    catch { return $false }
}
$ollama = Find-Ollama
if (-not $ollama) {
    Info "Installing Ollama"
    $installer = Join-Path $env:TEMP "OllamaSetup.exe"
    Download "https://ollama.com/download/OllamaSetup.exe" $installer
    $p = Start-Process $installer -ArgumentList "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES" -Wait -PassThru
    $ollama = Find-Ollama
    if (-not $ollama) { Fail "Ollama installation failed (exit code $($p.ExitCode))." }
}
if (-not (Test-Ollama)) {
    Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 60 -and -not (Test-Ollama); $i++) { Start-Sleep -Milliseconds 500 }
}
if (-not (Test-Ollama)) { Fail "Ollama is installed but did not start." }
Ok "Ollama ready ($ollama)"

# ------------------------------------------------------------------ 7. library + models
Step 7 "Downloading AI models"
$dataDir = Join-Path $InstallDir "data"
if ((Test-Library $ImportData) -and -not (Test-Library $dataDir)) {
    Info "Importing your existing library from $ImportData"
    robocopy $ImportData $dataDir /E /XD logs webview /NFL /NDL /NJH /NJS /NP | Out-Null
}
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$model = if ($vramMB -ge 14000) { "qwen3:14b" } elseif ($vramMB -ge 7000) { "qwen3:8b" } else { "qwen3:4b" }
# Keep a model the user already chose; otherwise record the one that fits this GPU.
$env:SR_INSTALL_MODEL = $model
$model = (& $venvPy -c @"
import json, os, pathlib
p = pathlib.Path(r'$dataDir') / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
s.setdefault('ollama_model', os.environ['SR_INSTALL_MODEL'])
p.write_text(json.dumps(s, indent=2), encoding='utf-8')
print(s['ollama_model'] if s.get('provider', 'ollama') == 'ollama' else '')
"@).Trim()
if ($model) {
    Info "Model: $model (chosen for $([math]::Round($vramMB / 1024)) GB of video memory)"
    Run $ollama @("pull", $model) "Downloading $model"
}
Info "Embedding model for search: BAAI/bge-small-en-v1.5"
Run $venvPy @("-c", "from sentence_transformers import SentenceTransformer as S; S('BAAI/bge-small-en-v1.5')") "Downloading embedding model"
Ok "Models ready"

# ------------------------------------------------------------------ 8. shortcuts + registration
Step 8 "Creating shortcuts"
$pyw = Join-Path $InstallDir "venv\Scripts\pythonw.exe"
$icon = Join-Path $InstallDir "assets\icon.ico"
$launcher = Join-Path $InstallDir "launcher.py"
$uninstall = Join-Path $InstallDir "uninstall.ps1"
$ws = New-Object -ComObject WScript.Shell
function New-Shortcut($path, $target, $arguments, $desc) {
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $target; $s.Arguments = $arguments; $s.WorkingDirectory = $InstallDir
    $s.IconLocation = "$icon,0"; $s.Description = $desc
    $s.Save()
}
$desktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Lorekeeper.lnk"
$menuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Lorekeeper"
New-Item -ItemType Directory -Force -Path $menuDir | Out-Null
New-Shortcut $desktopLnk $pyw "`"$launcher`"" "Lorekeeper - AI e-book reader"
New-Shortcut (Join-Path $menuDir "Lorekeeper.lnk") $pyw "`"$launcher`"" "Lorekeeper - AI e-book reader"
New-Shortcut (Join-Path $menuDir "Uninstall Lorekeeper.lnk") "powershell.exe" "-NoProfile -ExecutionPolicy Bypass -File `"$uninstall`"" "Remove Lorekeeper"

$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Lorekeeper"
New-Item -Path $key -Force | Out-Null
$props = @{
    DisplayName = "Lorekeeper"; DisplayVersion = $Version; Publisher = "Lorekeeper"
    DisplayIcon = $icon; InstallLocation = $InstallDir
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstall`""
}
foreach ($k in $props.Keys) { New-ItemProperty -Path $key -Name $k -Value $props[$k] -PropertyType String -Force | Out-Null }
New-ItemProperty -Path $key -Name NoModify -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $key -Name NoRepair -Value 1 -PropertyType DWord -Force | Out-Null
Ok "Desktop and Start Menu shortcuts created"

Write-Host ""
Write-Host "  Lorekeeper is installed. Open it any time from the desktop icon." -ForegroundColor Green
if (-not $NoLaunch) {
    # Launch through Explorer so the app never inherits elevated rights (drag & drop needs that).
    Start-Process explorer.exe -ArgumentList "`"$desktopLnk`""
}
Start-Sleep -Seconds 6
