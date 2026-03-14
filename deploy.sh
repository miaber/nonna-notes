#!/usr/bin/env bash
set -euo pipefail

REGION="us-central1"
BACKEND_SERVICE="mise-backend"
AGENT_SERVICE="mise-recipe-agent"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
  echo "Usage: ./deploy.sh [all|backend|agent|frontend]"
  echo "  all       Deploy backend + recipe-agent + frontend (default)"
  echo "  backend   Deploy backend only"
  echo "  agent     Deploy recipe-agent only"
  echo "  frontend  Build and deploy frontend only"
  exit 1
}

deploy_backend() {
  echo "==> Deploying backend..."
  (cd "$PROJECT_DIR/backend" && gcloud run deploy "$BACKEND_SERVICE" \
    --source . \
    --region "$REGION" \
    --allow-unauthenticated \
    --timeout 3600)
  echo "==> Backend deployed."
}

deploy_agent() {
  echo "==> Deploying recipe-agent..."
  (cd "$PROJECT_DIR/recipe-agent" && gcloud run deploy "$AGENT_SERVICE" \
    --source . \
    --region "$REGION" \
    --allow-unauthenticated)
  echo "==> Recipe-agent deployed."
}

deploy_frontend() {
  echo "==> Building frontend..."
  (cd "$PROJECT_DIR/frontend" && npm run build)
  echo "==> Deploying to Firebase Hosting..."
  (cd "$PROJECT_DIR" && firebase deploy --only hosting)
  echo "==> Frontend deployed."
}

TARGET="${1:-all}"

case "$TARGET" in
  all)
    deploy_backend &
    deploy_agent &
    wait
    deploy_frontend
    ;;
  backend)  deploy_backend ;;
  agent)    deploy_agent ;;
  frontend) deploy_frontend ;;
  *)        usage ;;
esac

echo "Done!"
