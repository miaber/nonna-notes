#!/usr/bin/env bash
# Run from repo root. Starts backend, recipe-agent, and frontend (one terminal).

set -e
ROOT="$(dirname "$0")/.."
cd "$ROOT"

# Free ports in case a previous run didn't exit cleanly
for port in 8000 8001 5173; do
  pid=$(lsof -ti:"$port" 2>/dev/null) && kill $pid 2>/dev/null || true
done
sleep 1

cleanup() {
  echo "Stopping services..."
  kill $BACKEND_PID $RECIPE_PID 2>/dev/null
  exit 0
}
trap cleanup SIGINT SIGTERM

(cd backend && source venv/bin/activate && uvicorn main:app --reload --port 8000) &
BACKEND_PID=$!

(cd recipe-agent && source venv/bin/activate && uvicorn main:app --reload --port 8001) &
RECIPE_PID=$!

(cd frontend && npm run dev)
kill $BACKEND_PID $RECIPE_PID 2>/dev/null || true
