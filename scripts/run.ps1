# Run from repo root. Starts backend, recipe-agent, and frontend in one terminal; all logs stream here.
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
  & .\venv\Scripts\uvicorn.exe main:app --reload --port 8000 2>&1
} -Name Backend

$recipeJob = Start-Job -ScriptBlock {
  Set-Location $using:Root
  Set-Location recipe-agent
  & .\venv\Scripts\uvicorn.exe main:app --reload --port 8001 2>&1
} -Name RecipeAgent

$frontendJob = Start-Job -ScriptBlock {
  Set-Location $using:Root
  Set-Location frontend
  npm run dev 2>&1
} -Name Frontend

$jobs = @($backendJob, $recipeJob, $frontendJob)
try {
  while ($jobs | Where-Object { $_.State -eq 'Running' }) {
    $jobs | Receive-Job
    Start-Sleep -Milliseconds 200
  }
  $jobs | Receive-Job
} finally {
  Write-Host "Stopping services..."
  $jobs | Stop-Job -ErrorAction SilentlyContinue
  $jobs | Remove-Job -Force -ErrorAction SilentlyContinue
}
