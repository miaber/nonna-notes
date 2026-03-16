# Nonna Notes

Real-time AI cooking companion powered by Gemini Live API. Nonna watches your kitchen through the camera, listens for questions, and responds with voice — walking you through recipes step-by-step or documenting your freestyle cooks as you go.

## Features

- **Voice-guided cooking** — Nonna reads recipe steps aloud and waits for you to say "next"
- **Recipe parsing** — paste any recipe URL or YouTube cooking video and get a structured recipe
- **Document mode** — cook without a recipe and Nonna records steps + ingredients as you go
- **Timers** — hands-free timer management via voice
- **Step photos** — Nonna prompts you to show your progress and captures photos
- **My Recipes** — save, browse, and re-cook your recipe library
- **Background music** — ask Nonna to play music while you cook
- **Easter egg** — tap the logo 5 times to unlock Gordon Ramsay mode

## Quick Start

**Prerequisites:** Python 3.10+, Node.js 18+, Chrome (required for camera + AudioWorklet APIs).

### 1. Get a Gemini API key

Get a key from [ai.google.dev](https://ai.google.dev). Make sure the **Generative Language API** is [enabled](https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com) and billing is active on the GCP project (required for the Live API).

Optionally, enable **YouTube Data API v3** on the same project for background music search.

### 2. Clone and install

```bash
git clone https://github.com/miaber/nonna-notes.git
cd nonna-notes

# Create .env in the project root
cat > .env <<'EOF'
GEMINI_API_KEY=your-gemini-key-here
YOUTUBE_API_KEY=your-youtube-key-here          # optional, for music
EOF

# Backend
cd backend && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt && cd ..

# Recipe Agent
cd recipe-agent && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt && cd ..

# Frontend
cd frontend && npm install && cd ..
```

### 3. Start all three services

Open three terminals:

```bash
# Terminal 1 — Backend (port 8000)
cd backend && source venv/bin/activate && uvicorn main:app --reload --port 8000

# Terminal 2 — Recipe Agent (port 8001)
cd recipe-agent && source venv/bin/activate && uvicorn main:app --reload --port 8001

# Terminal 3 — Frontend (port 5173)
cd frontend && npm run dev
```

### 4. Open the app

Go to **http://localhost:5173**, grant camera + microphone access, and click **Start Cooking**.

## Architecture

```
Browser (React + Vite)
  ├─ WebSocket  →  Backend (FastAPI)  →  Gemini Live API
  │                  (bidirectional audio + video streaming)
  └─ HTTP POST  →  Recipe Agent (FastAPI)
                     (URL/YouTube/image parsing via Gemini)
```

| Service | Port | Purpose |
|---|---|---|
| **backend** | 8000 | WebSocket proxy to Gemini Live API, recipe CRUD endpoints |
| **recipe-agent** | 8001 | Parses recipe URLs, YouTube videos, and uploaded images into structured recipes |
| **frontend** | 5173 | React SPA (Vite dev server) |

### Tech stack

- **Frontend:** React, Vite, AudioWorklet, Canvas
- **Backend:** Python, FastAPI, `google-genai` SDK (Gemini Live API)
- **Recipe Agent:** Python, FastAPI, `google-genai` SDK, BeautifulSoup4
- **Production:** Google Cloud Run, Firebase Hosting, Firebase Auth, Firebase Storage
