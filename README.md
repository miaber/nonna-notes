# Mise — Live Cooking Assistant

Real-time AI sous chef powered by Gemini Live API. Watches your kitchen through your camera, listens for your questions, and responds with voice.

## Setup

### Prerequisites
- Python 3.11+
- Node 18+
- A Gemini API key from [ai.google.dev](https://ai.google.dev) (billing may be required for Live API)
- Chrome (required — `ImageCapture` API is Chrome-only)

### Backend

```bash
cd backend
pip install -r requirements.txt
cp ../.env.example .env
# Edit .env and add your GEMINI_API_KEY
uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173, grant camera + mic access, click **Start Cooking**.

## Architecture

```
Browser (React)
  │  WebSocket (ws://localhost:8000/ws)
  ▼
FastAPI (Python)          ← keeps API key server-side
  │  google-genai SDK
  ▼
Gemini Live API
  (bidirectional audio + video streaming)
```

- Browser captures 16kHz PCM audio and 1fps JPEG frames
- Backend forwards both to Gemini Live API in real time
- Gemini responds with 24kHz PCM audio streamed back to browser
- Text transcripts sent as side-channel for display

## Known Constraints

- **Chrome only** — `ImageCapture` API not available in Firefox/Safari
- `createScriptProcessor` is deprecated; replace with `AudioWorklet` if issues arise
- CORS is open (`*`) for development — tighten before deploying
