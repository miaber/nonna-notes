#!/usr/bin/env bash
set -euo pipefail

REGION="us-central1"
BACKEND_SERVICE="mise-backend"
AGENT_SERVICE="mise-recipe-agent"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Firebase project config
FB_PROJECT_ID="gemini-hackathon-488004"
FB_API_KEY=$(grep '^VITE_FIREBASE_API_KEY=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2- || echo "")
FB_AUTH_DOMAIN="${FB_PROJECT_ID}.firebaseapp.com"
FB_STORAGE_BUCKET="${FB_PROJECT_ID}.firebasestorage.app"
FB_MESSAGING_SENDER_ID="514526787683"
FB_APP_ID="1:514526787683:web:aa8d83d9cf5d94ba886056"
ALLOWED_ORIGIN="https://${FB_PROJECT_ID}.web.app"

# Load API keys from .env (fallback to empty)
GEMINI_API_KEY=$(grep '^GEMINI_API_KEY=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2- || echo "")
YOUTUBE_API_KEY=$(grep '^YOUTUBE_API_KEY=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2- || echo "")
YT_TRANSCRIPT_KEY=$(grep '^YOUTUBE_TRANSCRIPT_DEV_API_KEY=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2- || echo "")

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
  AGENT_URL=$(gcloud run services describe "$AGENT_SERVICE" --region "$REGION" --format 'value(status.url)' 2>/dev/null || echo "")
  (cd "$PROJECT_DIR/backend" && gcloud run deploy "$BACKEND_SERVICE" \
    --source . \
    --region "$REGION" \
    --allow-unauthenticated \
    --timeout 3600 \
    --set-env-vars "USE_VERTEX_AI=false,GEMINI_API_KEY=${GEMINI_API_KEY},YOUTUBE_API_KEY=${YOUTUBE_API_KEY},FIREBASE_PROJECT_ID=${FB_PROJECT_ID},FIREBASE_STORAGE_BUCKET=${FB_STORAGE_BUCKET},ALLOWED_ORIGIN=${ALLOWED_ORIGIN},RECIPE_AGENT_URL=${AGENT_URL}")
  echo "==> Backend deployed."
}

deploy_agent() {
  echo "==> Deploying recipe-agent..."
  (cd "$PROJECT_DIR/recipe-agent" && gcloud run deploy "$AGENT_SERVICE" \
    --source . \
    --region "$REGION" \
    --allow-unauthenticated \
    --set-env-vars "USE_VERTEX_AI=true,GEMINI_API_KEY=${GEMINI_API_KEY},FIREBASE_STORAGE_BUCKET=${FB_STORAGE_BUCKET},YOUTUBE_TRANSCRIPT_DEV_API_KEY=${YT_TRANSCRIPT_KEY}")
  echo "==> Recipe-agent deployed."
}

deploy_frontend() {
  echo "==> Building frontend..."
  BACKEND_URL=$(gcloud run services describe "$BACKEND_SERVICE" --region "$REGION" --format 'value(status.url)')
  AGENT_URL=$(gcloud run services describe "$AGENT_SERVICE" --region "$REGION" --format 'value(status.url)')
  WS_URL=$(echo "$BACKEND_URL" | sed 's|^https://|wss://|')/ws
  (cd "$PROJECT_DIR/frontend" && \
    VITE_BACKEND_URL="$BACKEND_URL" \
    VITE_WS_URL="$WS_URL" \
    VITE_RECIPE_AGENT_URL="$AGENT_URL" \
    VITE_FIREBASE_API_KEY="$FB_API_KEY" \
    VITE_FIREBASE_AUTH_DOMAIN="$FB_AUTH_DOMAIN" \
    VITE_FIREBASE_PROJECT_ID="$FB_PROJECT_ID" \
    VITE_FIREBASE_STORAGE_BUCKET="$FB_STORAGE_BUCKET" \
    VITE_FIREBASE_MESSAGING_SENDER_ID="$FB_MESSAGING_SENDER_ID" \
    VITE_FIREBASE_APP_ID="$FB_APP_ID" \
    npm run build)
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
