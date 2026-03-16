# Run from repo root. Starts backend, recipe-agent, and frontend.
# Backend and recipe-agent open in separate windows (logs stream there). Frontend runs in this terminal.
# Usage: .\scripts\run.ps1   (from repo root)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Stop-ProcessOnPort {
  param([int]$Port)
  try {
    $conn = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    if ($conn) {
      $conn.OwningProcess | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    }
  } catch {}
}

Write-Host "Freeing ports 8000, 8001, 5173..."
8000, 8001, 5173 | ForEach-Object { Stop-ProcessOnPort $_ }
Start-Sleep -Seconds 1

$backendCmd = "Set-Location '$Root\backend'; .\venv\Scripts\uvicorn.exe main:app --reload --port 8000"
$recipeCmd = "Set-Location '$Root\recipe-agent'; .\venv\Scripts\uvicorn.exe main:app --reload --port 8001"

Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd -WindowStyle Normal
Start-Process powershell -ArgumentList "-NoExit", "-Command", $recipeCmd -WindowStyle Normal

try {
  Set-Location frontend
  npm run dev
} finally {
  Write-Host "Frontend stopped. Close the Backend and Recipe Agent windows to stop those services."
}
