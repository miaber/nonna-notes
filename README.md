# Nonna Notes

Real-time AI cooking companion powered by Gemini Live API. Nonna watches your kitchen through the camera, listens for questions, and responds with voice, walking you through recipes step-by-step or documenting your freestyle cooks as you go.

## Features

- **Voice-guided cooking:** Nonna reads recipe steps aloud and waits for you to say "next"
- **Recipe parsing:** Paste any recipe URL or YouTube cooking video and get a structured recipe
- **Document mode:** Cook without a recipe and Nonna records steps and ingredients as you go
- **Timers:** Hands-free timer management via voice
- **Step photos:** Nonna prompts you to show your progress and captures photos
- **My Recipes:** Save, browse, and re-cook your recipe library
- **Background music:** Ask Nonna to play music while you cook
- **Easter egg:** Tap the logo 5 times to unlock Gordon Ramsay mode

## Quick Start

**Prerequisites:** Python 3.10+, Node.js 18+, and a modern browser (Chrome, Firefox, or Safari) for camera and microphone.

### 1. Get API keys

You need **GEMINI_API_KEY** (required) and optionally **YOUTUBE_API_KEY**. In [Google Cloud Console](https://console.cloud.google.com): enable **Generative Language API** and billing; optionally **YouTube Data API v3**. Create an API key under **APIs & Services**, **Credentials**. Keys can be from any GCP project. ([Alternative: Gemini key from Google AI Studio](https://ai.google.dev) if that project has the API enabled and billing on.)

### 2. Setup (one command)

**macOS / Linux:**

```bash
git clone https://github.com/miaber/nonna-notes.git
cd nonna-notes
./scripts/setup.sh
```

Then edit `.env` in the project root and add your keys. (Setup creates `.env` from `.env.example` if it doesn't exist.)

**Windows (PowerShell, from repo root):**

```powershell
git clone https://github.com/miaber/nonna-notes.git
cd nonna-notes
.\scripts\setup.ps1
```

Then edit `.env` in the project root and add your keys.

### 3. Run

**macOS / Linux (one terminal):**

```bash
./scripts/run.sh
```

Then open **http://localhost:5173**, grant camera and microphone, and click **Start Cooking**. Press Ctrl+C to stop all services.

**Windows (one terminal, from repo root):** `.\scripts\run.ps1` then open http://localhost:5173. Press Ctrl+C to stop. Or run manually in three terminals: backend (`cd backend`, `venv\Scripts\activate`, `uvicorn main:app --reload --port 8000`), recipe-agent (same with `recipe-agent`, port 8001), frontend (`cd frontend`, `npm run dev`).

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
