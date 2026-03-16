# Run from repo root. Starts backend, recipe-agent, and frontend (one terminal).
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

$backendJob = Start-Job -ScriptBlock {
  Set-Location $using:Root
  Set-Location backend
  & .\venv\Scripts\uvicorn.exe main:app --reload --port 8000
} -Name Backend

$recipeJob = Start-Job -ScriptBlock {
  Set-Location $using:Root
  Set-Location recipe-agent
  & .\venv\Scripts\uvicorn.exe main:app --reload --port 8001
} -Name RecipeAgent

try {
  Set-Location frontend
  npm run dev
} finally {
  Write-Host "Stopping services..."
  Stop-Job $backendJob, $recipeJob -ErrorAction SilentlyContinue
  Remove-Job $backendJob, $recipeJob -Force -ErrorAction SilentlyContinue
}
