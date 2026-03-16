# Nonna Notes

Real-time AI cooking companion powered by Gemini Live API. Nonna watches your kitchen through the camera, listens for questions, and responds with voice, walking you through recipes step-by-step or documenting your freestyle cooks as you go.

## Features

- **Recipe parsing:** Paste any recipe URL, picture, or YouTube cooking video to get a structured recipe
- **Voice-guided cooking:** Nonna reads recipe steps aloud
- **Document mode:** Cook without a recipe and Nonna records steps and ingredients as you go
- **Timers:** Hands-free timer management via voice
- **Step photos:** Nonna prompts you to show your progress and captures photos
- **My Recipes:** Save, browse, and re-cook your recipe library
- **Background music:** Ask Nonna to play music while you cook

## Quick Start

### 1. Get API keys

You need **GEMINI_API_KEY** (required) and optionally **YOUTUBE_API_KEY**.

1. Open [Google Cloud Console](https://console.cloud.google.com) and create or select a project.
2. For your project, enable **[Generative Language API](https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com)** (required for Gemini) and turn on **billing** (required for the Live API).
3. For your project, enable **[YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com)** if you want music search or YouTube recipe URLs.
4. In **APIs & Services**, **Credentials**, create an API key. Use it as `GEMINI_API_KEY`; you can use the same key for `YOUTUBE_API_KEY` if both APIs are enabled in your project. You only need two keys if you want to restrict access (e.g. one key for Gemini, one for YouTube).

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

Then edit `.env` in the project root and add your keys.

### 3. Run


```bash
./scripts/run.sh
```

```powershell
.\scripts\run.ps1
```

Then open **http://localhost:5173**, grant camera and microphone, and click **Start Cooking**. Press Ctrl+C to stop all services.

Local dev skips auth (guest mode).
