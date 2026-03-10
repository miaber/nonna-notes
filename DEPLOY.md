# Deployment Guide

Two services + one hosting platform:

| Service | Platform | URL |
|---|---|---|
| `backend` (FastAPI + Gemini Live proxy) | Cloud Run | `wss://backend-xxx.run.app/ws` |
| `recipe-agent` (recipe parser) | Cloud Run | `https://recipe-agent-xxx.run.app` |
| Frontend (React SPA) | Firebase Hosting | `https://YOUR_PROJECT.web.app` |
| Recipes + photos | Cloud Storage (GCS) | bucket: `YOUR_PROJECT-recipes` |

---

## Prerequisites

```bash
# Install CLIs
brew install google-cloud-sdk firebase-tools   # or apt-get / winget equivalents

# Authenticate
gcloud auth login
gcloud auth configure-docker
firebase login
```

---

## 1 — GCP project setup (one-time)

```bash
PROJECT=your-gcp-project-id   # set this once; reuse below
gcloud config set project $PROJECT

# Enable APIs
gcloud services enable run.googleapis.com \
    cloudbuild.googleapis.com \
    storage.googleapis.com \
    artifactregistry.googleapis.com

# Create GCS bucket for recipe storage
gsutil mb -l us-central1 gs://${PROJECT}-recipes

# Create a long-lived access token (any random string)
ACCESS_TOKEN=$(openssl rand -hex 32)
echo "ACCESS_TOKEN: $ACCESS_TOKEN"   # save this — you'll need it for the frontend too
```

---

## 2 — Deploy `recipe-agent`

```bash
cd recipe-agent

gcloud run deploy recipe-agent \
  --source . \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars "GEMINI_API_KEY=YOUR_GEMINI_API_KEY" \
  --port 8001 \
  --min-instances 0 \
  --max-instances 2

# Note the URL, e.g.: https://recipe-agent-abc123-uc.a.run.app
RECIPE_AGENT_URL=https://recipe-agent-abc123-uc.a.run.app
```

**Optional env vars for recipe-agent:**
- `YOUTUBE_API_KEY` — include video title and description when parsing YouTube URLs (same key as backend music search).
- **Recipe parse cache:** Set `FIREBASE_STORAGE_BUCKET` (same as backend, e.g. `your-project.appspot.com`) so the recipe-agent persists URL → parsed recipe in Firebase Storage (`cache/{key}.json`). Shared across instances and survives restarts. Without it, cache is in-memory only (per instance, lost on restart). Optional: `RECIPE_AGENT_CACHE_TTL_SEC` (default 86400 = 24h), `RECIPE_AGENT_CACHE_MAX` (default 200, in-memory fallback only).
- **YouTube transcript on Cloud Run:** YouTube blocks cloud IPs. To get transcripts from Cloud Run you can either use a **free transcript API** (recommended) or a **proxy** (see below). Without either, description-only fallback works if `YOUTUBE_API_KEY` is set.

### Getting YouTube transcripts from Cloud Run (no proxy required)

**Option 1 — youtubetranscript.dev (free tier, no proxy)**

1. Sign up at [youtubetranscript.dev](https://www.youtubetranscript.dev/) (no credit card for free tier).
2. In the [dashboard](https://www.youtubetranscript.dev/dashboard/account) create or copy an API token.
3. Set it when deploying recipe-agent:
   ```bash
   --set-env-vars "...,YOUTUBE_TRANSCRIPT_DEV_API_KEY=your_bearer_token"
   ```
   Free plan: **100 transcript extractions per month**. Their servers fetch the transcript, so it works from Cloud Run with no proxy. If the key is set, we try this first; if it fails (e.g. out of credits), we fall back to the library (and then description).

### Setting up a proxy for YouTube transcripts (optional)

If you prefer not to use the transcript API, you can use a proxy so the Python library’s requests come from a non-cloud IP. Two ways:

**Option A — Webshare (rotating residential, paid)**

1. Sign up at [Webshare](https://www.webshare.io/).
2. Buy a **“Residential”** proxy package (not “Proxy Server” or “Static Residential”). Pick a size that fits your traffic.
3. In the dashboard go to [Proxy Settings](https://dashboard.webshare.io/proxy/settings) and copy your **Proxy Username** and **Proxy Password**.
4. Deploy recipe-agent with those as env vars:
   ```bash
   gcloud run deploy recipe-agent ... \
     --set-env-vars "GEMINI_API_KEY=...,YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME=your_username,YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD=your_password"
   ```
   Or in Cloud Console: recipe-agent → Edit & deploy new revision → Variables → add the two variables (mark Password as “Secret” if you want).

**Option B — Any HTTP proxy (generic)**

If you have another proxy provider (Bright Data, Oxylabs, a VPS with Squid, etc.), they’ll give you a URL like:

- `http://proxy.example.com:8080`
- or `http://username:password@proxy.example.com:8080`

Set it as:

```bash
--set-env-vars "...,YOUTUBE_TRANSCRIPT_PROXY=http://user:pass@your-proxy-host:port"
```

Use the same URL for both HTTP and HTTPS; the app uses it for YouTube’s HTTPS traffic.

**If you skip both transcript API and proxy**

- Transcripts from Cloud Run will usually fail (YouTube blocks cloud IPs).
- You can still rely on **video description**: set `YOUTUBE_API_KEY`. The app will use the video’s title and description as the recipe source when the transcript is blocked.

---

## 3 — Deploy `backend`

WebSocket sessions can be long-running — set timeout to 1 hour.

```bash
cd backend

gcloud run deploy mise-backend \
  --source . \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars "GEMINI_API_KEY=YOUR_GEMINI_API_KEY,ACCESS_TOKEN=${ACCESS_TOKEN},GCS_BUCKET=${PROJECT}-recipes,FIREBASE_STORAGE_BUCKET=YOUR_PROJECT.appspot.com,ALLOWED_ORIGIN=https://YOUR_PROJECT.web.app,RECIPE_AGENT_URL=${RECIPE_AGENT_URL}" \
  --port 8000 \
  --timeout 3600 \
  --min-instances 0 \
  --max-instances 3

# Note the URL, e.g.: https://mise-backend-abc123-uc.a.run.app
BACKEND_URL=https://mise-backend-abc123-uc.a.run.app
```

> **Recipe persistence:** Set `FIREBASE_STORAGE_BUCKET` to your Firebase Storage bucket (e.g. `your-project-id.appspot.com`, same as `VITE_FIREBASE_STORAGE_BUCKET` in the frontend). Without it, saved recipes are written to the container’s local disk and are lost when the instance scales down or another instance handles the request — so “My Recipes” will stay empty after saving.

> **GCS permissions**: Cloud Run uses the Compute Engine default service account.
> Grant it Storage Object Admin on the bucket:
> ```bash
> PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
> gsutil iam ch serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com:roles/storage.objectAdmin gs://${PROJECT}-recipes
> ```

---

## 4 — Firebase console setup (one-time)

1. Go to [console.firebase.google.com](https://console.firebase.google.com) → select your GCP project
2. **Authentication → Get started → Sign-in method → Google → Enable → Save**
3. In **Authentication → Settings → Authorized domains** add your Firebase Hosting domain (e.g. `your-project.web.app`) — it's usually already there
4. Go to **Project settings → Your apps → Add app (web icon `</>`)** → get the `firebaseConfig` object

---

## 5 — Build & deploy frontend

```bash
cd frontend

# Get your Firebase config from step 4 above and fill in the values:
cat > .env.production << EOF
VITE_WS_URL=wss://mise-backend-abc123-uc.a.run.app/ws
VITE_BACKEND_URL=https://mise-backend-abc123-uc.a.run.app
VITE_FIREBASE_API_KEY=AIza...
VITE_FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
VITE_FIREBASE_PROJECT_ID=your-project-id
VITE_FIREBASE_STORAGE_BUCKET=your-project.appspot.com
VITE_FIREBASE_MESSAGING_SENDER_ID=123456789
VITE_FIREBASE_APP_ID=1:123456789:web:abc123
EOF

npm run build

# Edit .firebaserc and replace YOUR_FIREBASE_PROJECT_ID with your actual Firebase project ID

firebase deploy --only hosting
```

Firebase Hosting URL will be printed. It matches `ALLOWED_ORIGIN` set in step 3.

Also set `FIREBASE_PROJECT_ID` on the backend (so it verifies Firebase tokens):
```bash
gcloud run services update mise-backend \
  --region us-central1 \
  --set-env-vars "FIREBASE_PROJECT_ID=your-project-id"
```

---

## 6 — Verify

```bash
# Health checks
curl https://mise-backend-abc123-uc.a.run.app/health
curl https://recipe-agent-abc123-uc.a.run.app/health

# Authenticated endpoint
curl -H "Authorization: Bearer $ACCESS_TOKEN" https://mise-backend-abc123-uc.a.run.app/recipes
```

---

## Re-deploying after code changes

```bash
# Backend only
cd backend && gcloud run deploy mise-backend --source . --region us-central1

# Recipe agent only
cd recipe-agent && gcloud run deploy recipe-agent --source . --region us-central1

# Frontend only
cd frontend && npm run build && firebase deploy --only hosting
```

---

## Local dev (unchanged)

```bash
# Backend
cd backend && uvicorn main:app --reload

# Recipe agent
cd recipe-agent && uvicorn main:app --port 8001 --reload

# Frontend
cd frontend && npm run dev
```

No `ACCESS_TOKEN` or `GCS_BUCKET` needed locally — both default to disabled.
