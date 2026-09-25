# Builds dist\Lorekeeper-Setup.exe: a single self-extracting installer made with IExpress
# (built into Windows). The exe carries the app source (~200 KB); Python packages, PyTorch,
# Ollama and the AI model are downloaded during installation.
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dist = Join-Path $root "dist"
$stage = Join-Path $dist "stage"
$tree = Join-Path $dist "tree"

Remove-Item -Recurse -Force $stage, $tree -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage, $tree | Out-Null

python (Join-Path $PSScriptRoot "make_icon.py") | Out-Null

foreach ($d in "app", "static", "assets", "installer") {
    robocopy (Join-Path $root $d) (Join-Path $tree $d) /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
}
foreach ($f in "launcher.py", "requirements.txt", "README.md") { Copy-Item (Join-Path $root $f) $tree }
# Antivirus often scans freshly copied files and briefly locks them: retry the zip.
for ($i = 1; $i -le 5; $i++) {
    try {
        Compress-Archive -Path (Join-Path $tree "*") -DestinationPath (Join-Path $stage "app.zip") -Force -ErrorAction Stop
        break
    } catch {
        if ($i -eq 5) { throw }
        Start-Sleep -Seconds 2
    }
}

Copy-Item (Join-Path $PSScriptRoot "install.ps1") $stage
Set-Content -Path (Join-Path $stage "setup.cmd") -Encoding Ascii -Value @'
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
'@

$target = Join-Path $dist "Lorekeeper-Setup.exe"
$sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQuietInstCmd=%AdminQuietInstCmd%
UserQuietInstCmd=%UserQuietInstCmd%
SourceFiles=SourceFiles
[Strings]
InstallPrompt=
DisplayLicense=
FinishMessage=
TargetName=$target
FriendlyName=Lorekeeper Setup
AppLaunched=cmd /c setup.cmd
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
FILE0="setup.cmd"
FILE1="install.ps1"
FILE2="app.zip"
[SourceFiles]
SourceFiles0=$stage\
[SourceFiles0]
%FILE0%=
%FILE1%=
%FILE2%=
"@
$sedPath = Join-Path $stage "setup.sed"
Set-Content -Path $sedPath -Value $sed -Encoding Ascii
Remove-Item $target -Force -ErrorAction SilentlyContinue
# IExpress must run from the staging folder (it writes its temporary cabinet to the working dir).
$p = Start-Process "$env:SystemRoot\System32\iexpress.exe" -ArgumentList "/N /Q setup.sed" -WorkingDirectory $stage -Wait -PassThru
if (-not (Test-Path $target)) { throw "IExpress failed (exit $($p.ExitCode))" }
Remove-Item -Recurse -Force $tree
Write-Host "Built $target ($([math]::Round((Get-Item $target).Length / 1KB)) KB)"
