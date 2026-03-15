# Nonna Notes - Cook with Nonna

Real-time AI cooking companion powered by Gemini Live API. Nonna watches your kitchen through the camera, listens for questions, and responds with voice - walking you through recipes step-by-step or documenting your freestyle cooks as you go.

## Features

- **Voice-guided cooking** - Nonna reads recipe steps aloud and waits for you to say "next"
- **Recipe parsing** - paste any recipe URL or YouTube cooking video and get a structured recipe
- **Document mode** - cook without a recipe and Nonna records steps + ingredients as you go
- **Timers** - hands-free timer management via voice
- **Step photos** - Nonna prompts you to show your progress and captures photos
- **My Recipes** - save, browse, and re-cook your recipe library
- **Background music** - ask Nonna to play music while you cook
- **Easter egg** - tap the logo 5 times to unlock Gordon Ramsay mode

## Quick Start

**Only requirement: a Gemini API key.** Everything else (Firebase, YouTube API, etc.) is optional and only needed for the full experience or production deployment.

### 1. Get API keys

| Key | Required | Where to get it | What to enable on the GCP project |
|---|---|---|---|
| **Gemini API key** | **Yes** | [ai.google.dev](https://ai.google.dev) - sign in and create a key | **Generative Language API** must be enabled ([link](https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com)). The Live API also requires billing enabled on the project. |
| **YouTube Data API key** | Recommended | Same GCP project, create/reuse an API key | **YouTube Data API v3** must be enabled ([link](https://console.cloud.google.com/apis/library/youtube.googleapis.com)). Powers background music search and YouTube recipe video metadata. |
| **YouTube Transcript API key** | Optional | [youtubetranscript.dev](https://youtubetranscript.dev) - sign up for an API key | No GCP setup needed - this is a third-party service. Used as a fallback for fetching YouTube video transcripts (especially on cloud deployments where the direct library is blocked). |
| **Firebase API key** | For deploy | Firebase console → Project settings → Web app | Used by the frontend for Google Sign-In. Run `firebase apps:sdkconfig web` to get it. Only needed for production deployment with user auth. |

> **Common gotcha:** If the Generative Language API is not enabled for the GCP project tied to your Gemini key, the app will fail to connect (you'll see repeated retry attempts). Double-check it's enabled at the link above.

### 2. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/nonna-notes.git
cd nonna-notes

# Create a single .env in the project root (shared by all services)
cat > .env <<'EOF'
GEMINI_API_KEY=your-gemini-key-here
YOUTUBE_API_KEY=your-youtube-key-here
YOUTUBE_TRANSCRIPT_DEV_API_KEY=your-transcript-key-here
FB_API_KEY=your-firebase-api-key-here
EOF

# Backend
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd ..

# Recipe Agent
cd recipe-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd ..

# Frontend
cd frontend
npm install
cd ..
```

### 3. Start all three services

Open three terminal windows:

```bash
# Terminal 1 - Backend
cd backend && source venv/bin/activate && uvicorn main:app --reload --port 8000

# Terminal 2 - Recipe Agent
cd recipe-agent && source venv/bin/activate && uvicorn main:app --reload --port 8001

# Terminal 3 - Frontend
cd frontend && npm run dev
```

### 4. Open the app

Go to **http://localhost:5173**, grant camera + microphone access, and click **Start Cooking**.

> **Note:** Chrome or Edge recommended. The app uses `getUserMedia` for camera/mic and `AudioWorklet` for audio processing.

## Architecture

```
Browser (React + Vite)
  ├─ WebSocket  →  Backend (FastAPI)  →  Gemini Live API
  │                  (bidirectional audio + video streaming)
  └─ HTTP POST  →  Recipe Agent (FastAPI)
                     (URL/YouTube parsing, recipe generation via Gemini)
```

Three services:

| Service | Port | Purpose |
|---|---|---|
| **backend** | 8000 | WebSocket proxy to Gemini Live API, REST endpoints for recipes/pantry |
| **recipe-agent** | 8001 | Parses recipe URLs, YouTube videos, or generates recipes from a description |
| **frontend** | 5173 | React SPA (Vite dev server) |

### Tech stack

- **Frontend:** React, Vite, AudioWorklet (16 kHz PCM capture), Canvas (JPEG frame capture)
- **Backend:** Python, FastAPI, WebSocket, `google-genai` SDK (Gemini Live API)
- **Recipe Agent:** Python, FastAPI, `google-genai` SDK, BeautifulSoup4, `youtube-transcript-api`
- **Google Cloud (production):** Cloud Run (backend + recipe-agent), Firebase Hosting (frontend), Firebase Auth, Firebase Storage

## How It Works

1. Browser captures 16 kHz PCM audio via AudioWorklet and ~0.5 fps JPEG frames via canvas
2. Backend forwards both to Gemini Live API over a persistent WebSocket
3. Gemini responds with 24 kHz PCM audio streamed back to the browser
4. Gemini calls tools (complete_step, set_timer, capture_step_photo, etc.) that the backend translates into UI updates sent to the frontend
5. Text transcripts are sent as a side-channel for display
6. Recipe parsing happens in the dedicated recipe-agent service (URL scraping, YouTube transcript extraction, or Gemini generation with Google Search grounding)

## Environment Variables

All services read from a single `.env` file in the project root. **Only `GEMINI_API_KEY` is required.**

### Root `.env`

| Variable | Required | Used by | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | **Yes** | Backend, Recipe Agent | Google AI Studio API key (needs **Generative Language API** enabled) |
| `YOUTUBE_API_KEY` | Recommended | Backend | YouTube Data API v3 key (needs **YouTube Data API v3** enabled) - enables music playback |
| `YOUTUBE_TRANSCRIPT_DEV_API_KEY` | Optional | Recipe Agent | API key from [youtubetranscript.dev](https://youtubetranscript.dev) - fetches YouTube transcripts (recommended for cloud deployments) |
| `ACCESS_TOKEN` | No | Backend | Simple auth token for local dev (ignored when Firebase auth is configured) |
| `FIREBASE_PROJECT_ID` | No | Backend | Enables Firebase ID token verification |
| `FIREBASE_STORAGE_BUCKET` | No | Backend, Recipe Agent | GCS bucket for persistent recipe/cache storage (without this, saves to local JSON files) |
| `RECIPE_AGENT_URL` | No | Backend | Override recipe agent URL (default: `http://localhost:8001`) |
| `ALLOWED_ORIGIN` | No | Backend | CORS origin (default: `*`) |

### Frontend

No `.env` file needed for local development. The frontend defaults to `localhost:8000` for the backend and `localhost:8001` for the recipe agent.

For production with Firebase auth, set: `VITE_FIREBASE_API_KEY`, `VITE_FIREBASE_AUTH_DOMAIN`, `VITE_FIREBASE_PROJECT_ID`, `VITE_FIREBASE_STORAGE_BUCKET`, `VITE_FIREBASE_MESSAGING_SENDER_ID`, `VITE_FIREBASE_APP_ID`.

Override backend URLs with `VITE_BACKEND_URL`, `VITE_WS_URL`, `VITE_RECIPE_AGENT_URL`.

## Production Deployment

See [DEPLOY.md](DEPLOY.md) for full Google Cloud deployment instructions (Cloud Run + Firebase Hosting).

## Known Constraints

- CORS is open (`*`) for local development - restricted via `ALLOWED_ORIGIN` in production
- YouTube transcript extraction may fail from cloud IPs (see [DEPLOY.md](DEPLOY.md) for proxy options)
