# Run from repo root. Creates .env if missing, then installs backend, recipe-agent, and frontend.
# Usage: .\scripts\setup.ps1

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host "Created .env from .env.example. Edit .env and add your GEMINI_API_KEY (and optionally YOUTUBE_API_KEY)."
}

Write-Host "Installing backend..."
Set-Location backend
python -m venv venv
& .\venv\Scripts\pip.exe install -r requirements.txt
Set-Location $Root

Write-Host "Installing recipe-agent..."
Set-Location recipe-agent
python -m venv venv
& .\venv\Scripts\pip.exe install -r requirements.txt
Set-Location $Root

Write-Host "Installing frontend..."
Set-Location frontend
npm install
Set-Location $Root

Write-Host "Done. Add your API keys to .env, then run: .\scripts\run.ps1"
