#!/usr/bin/env bash
# Run from repo root. Creates .env if missing, then installs backend, recipe-agent, and frontend.

set -e
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit .env and add your GEMINI_API_KEY (and optionally YOUTUBE_API_KEY)."
fi

echo "Installing backend..."
(cd backend && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt)

echo "Installing recipe-agent..."
(cd recipe-agent && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt)

echo "Installing frontend..."
(cd frontend && npm install)

echo "Done. Add your API keys to .env, then run: ./scripts/run.sh"
