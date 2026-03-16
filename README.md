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

You need **GEMINI_API_KEY** (required) and optionally **YOUTUBE_API_KEY**.

1. Open [Google Cloud Console](https://console.cloud.google.com) and create or select a project.
2. Enable **[Generative Language API](https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com)** for that project (required for Gemini). Turn on **billing** for the project (required for the Live API).
3. Optionally enable **[YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com)** for music and YouTube recipe parsing.
4. Go to **APIs & Services**, **Credentials**, **Create credentials**, **API key**. Use it as `GEMINI_API_KEY`; you can use the same key for `YOUTUBE_API_KEY` if both APIs are enabled.


### 2. Setup

**macOS / Linux**

```bash
git clone https://github.com/miaber/nonna-notes.git
cd nonna-notes
./scripts/setup.sh
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/miaber/nonna-notes.git
cd nonna-notes
.\scripts\setup.ps1
```

Then edit `.env` in the project root and add your keys. (Setup creates `.env` from `.env.example` if it doesn't exist.)

### 3. Run

**macOS / Linux**

```bash
./scripts/run.sh
```

**Windows (PowerShell)**

```powershell
.\scripts\run.ps1
```

Then open **http://localhost:5173**, grant camera and microphone, and click **Start Cooking**. Press Ctrl+C to stop all services.

Local dev skips auth (guest mode).

## Tech stack

- **Frontend:** React, Vite, AudioWorklet, Canvas
- **Backend:** Python, FastAPI, `google-genai` SDK (Gemini Live API)
- **Recipe Agent:** Python, FastAPI, `google-genai` SDK, BeautifulSoup4
- **Production:** Google Cloud Run, Firebase Hosting, Firebase Auth, Firebase Storage
