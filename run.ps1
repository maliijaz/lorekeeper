# Starts Lorekeeper on http://127.0.0.1:8765
param([int]$Port = 8765)
Set-Location $PSScriptRoot
Start-Process "http://127.0.0.1:$Port"
python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
