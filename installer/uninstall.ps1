# Lorekeeper uninstaller. Removes the app, shortcuts and registration.
# Ollama, Python and downloaded AI models are shared with other programs and are left in place.
param([string]$Dir = "", [switch]$Relaunched)

$Host.UI.RawUI.WindowTitle = "Uninstall Lorekeeper"
if (-not $Dir) { $Dir = $PSScriptRoot }

# Run from a temp copy so the install folder (which contains this script) can be deleted.
if (-not $Relaunched) {
    $tmp = Join-Path $env:TEMP "Lorekeeper-uninstall.ps1"
    Copy-Item $PSCommandPath $tmp -Force
    Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$tmp`" -Dir `"$Dir`" -Relaunched"
    exit
}

Write-Host ""
Write-Host "  Uninstall Lorekeeper from $Dir" -ForegroundColor White
$answer = Read-Host "  Continue? (Y/n)"
if ($answer -match "^[nN]") { exit }
$keep = Read-Host "  Keep your library, reading progress and codex data? (Y/n)"
$keepData = -not ($keep -match "^[nN]")

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$Dir*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) "Lorekeeper.lnk") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Lorekeeper") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Lorekeeper" -Recurse -Force -ErrorAction SilentlyContinue

if (Test-Path $Dir) {
    Get-ChildItem $Dir -Force | Where-Object { -not ($keepData -and $_.Name -eq "data") } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    if (-not $keepData) { Remove-Item $Dir -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host ""
Write-Host "  Lorekeeper has been removed." -ForegroundColor Green
if ($keepData) { Write-Host "  Your library was kept in $Dir\data (reinstalling picks it up again)." -ForegroundColor DarkGray }
Write-Host "  Ollama and its models were left installed. To free their space: ollama rm <model>, or uninstall Ollama in Settings > Apps." -ForegroundColor DarkGray
Read-Host "  Press Enter to close"
