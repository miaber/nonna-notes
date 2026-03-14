# Learnings

Things we discovered building Nonna Notes. Merge with other machine's notes.

## Gemini Live API

- Gemini Live API sends speech and tool calls in **separate response objects**. Typical flow: response N has speech, response N+1 has `turn_complete`, response N+2 has `tool_call`. A per-response `spoke_in_this_response` flag is always False by the time the tool call arrives. Fix: use `sent_text_this_turn` (which is intentionally preserved across `turn_complete` via `pending_turn_complete`) to detect if the model already spoke before calling a step-navigation tool.
- Gemini often speaks the step instruction BEFORE calling `jump_to_step`/`complete_step`, then tries to speak it AGAIN (in personality voice) after the tool result. It can also speak twice AFTER the tool in two separate turns (e.g. once reading the step, once asking about a timer - same content rephrased). Fix: speech budget system that allows exactly 1 turn of model speech after a step tool. The budget decrements on turn_complete and auto-expires after 6 seconds (safety valve). Cleared on user interruption or non-step tool calls.
- **`suppress_until_turn_complete` and `_step_speech_budget` interact badly.** Both must be False for audio to pass. If the model speaks before a step tool call (e.g. "Certo, tesoro!" before `jump_to_step`), `suppress_until_turn_complete=True` blocks the model's NEXT turn (the step reading), even though `budget=1` should allow it. Fix: always clear `suppress_until_turn_complete = False` for step tools. The budget alone controls post-step speech.
- **Pre-tool filler speech ("Let me check") shows as text but no audio.** Gemini generates filler phrases before tool calls despite prompt instructions to call tools silently. The transcript text is sent to the browser before the tool call is detected (they arrive in the same response, text first). Retracting the transcript is too aggressive - it also removes meaningful speech like "Want me to set a timer?" before `set_timer`. Accepted as a minor cosmetic issue.
- **Model bundles too many actions in one turn** after reading a step (step + timer question + photo request + music suggestion). Prompt must be very explicit about ordering: read step, ask about timer, then STOP. Photo prompts only after user responds. Music suggestions only at session start.
- The drain loop that reads initial browser audio before starting the main send/recv tasks can get stuck if the browser is continuously streaming. Cap iterations (we use 20).
- Gemini can go silent after a burst of activity (speech + tool calls). No reliable fix - user just needs to refresh.
- **1011 "Deadline expired" disconnects** from the Gemini Live API are common and unpredictable. The reconnect mechanism works when only the Gemini connection drops (browser WS stays alive), but when both connections die simultaneously, the backend can't send the "reconnecting" indicator to the frontend. User sees silence then session ends.
- The `complete_step` tool guard needs to be lenient; blocking too aggressively (e.g. requiring high elapsed time or many turns) causes Nonna to skip step completions entirely.

## Step Highlighting

- Step IDs from the LLM (`step.id`) are not guaranteed to be sequential 1-based integers, even when the schema says `id: int`. The model sometimes generates arbitrary IDs.
- Fix: normalize step IDs to `i + 1` on the frontend when receiving the recipe, AND use enumeration index (not `step['id']`) when formatting the system prompt in the backend.

## Recipe Image Matching

- JSON-LD `HowToStep` entries sometimes include an `image` field - use these first as they're authoritative.
- When JSON-LD step images aren't available, fall back to matching blog post content images to steps.
- Image `alt` text on recipe blogs is often generic (e.g. "chocolate chip cookies process shot 3"). The paragraph text *immediately before* the image is much more descriptive of the action shown.
- Naive keyword overlap matching assigns wrong images because common dish words (e.g. "chocolate", "cookies") appear everywhere. Key fixes:
  - **Enforce page order**: image-to-step assignments must be monotonic (earlier images go to earlier steps).
  - **Weight action words**: process verbs ("mix", "fold", "add") score 2x vs generic nouns.
  - **Filter finished-dish photos**: exclude images whose text contains "finished", "plated", "stacked", etc.
  - **Use preceding paragraph context**: combine alt text with the `<p>` text before the image for matching.
  - **Lower threshold to 2.0**: monotonic ordering is the main false-positive guard, so the keyword threshold can be lower.
  - Walk `_preceding_text` all the way up to the content container (not just first `<figure>`/`<div>`) to handle nested wrappers like `<div class="wp-block-image"><figure><img></figure></div>`.

## Firebase Storage (not Firestore)

- We store recipes as individual JSON blobs in Firebase Storage, not Firestore documents. Each recipe has an `id` field.
- Drafts and saved recipes live in the same storage namespace. Draft-to-save conversion reuses the draft ID to prevent orphaned duplicates.
- Photos are stored as base64 strings inside the recipe JSON blob. Works for hackathon scale but won't scale well.

## Recipe Parsing

- `youtube-transcript-api` Python library often fails behind corporate proxies or on Cloud Run. We fall back to `youtubetranscript.dev` third-party API.
- The `GEMINI_API_KEY` must belong to a GCP project with the **Generative Language API** enabled, or all parsing calls fail with 403. When switching API keys, remember to enable the API on the new project and update both Cloud Run services (`gcloud run services update ... --update-env-vars`).
- `YOUTUBE_API_KEY` is used for music search/playback (YouTube Data API v3). `YOUTUBE_TRANSCRIPT_DEV_API_KEY` is a separate key for the third-party transcript service.

## Deployment

- `gcloud run deploy --source .` zips the current directory. Virtual environments with old file timestamps cause `ZIP does not support timestamps before 1980` errors. Fix: add `.gcloudignore` excluding `venv/`, `__pycache__/`, `.env`.
- Firebase Hosting requires a `"site"` field in `firebase.json` when the project has multiple sites.
- Cloud Run service names were originally `mise-backend` and `mise-recipe-agent` - kept those to avoid redeploying.

## Frontend

- `navigator.mediaDevices.getUserMedia` works across all modern browsers now (Chrome, Firefox, Safari).
- `createScriptProcessor` is deprecated but `AudioWorklet` is the replacement we use.
- `ResizeObserver` is useful for dynamically matching heights between elements (e.g. conversation panel matching camera height).

## Environment Variables

- Consolidated all env vars into a single root `.env` file. Both Python services use `load_dotenv(Path(__file__).resolve().parent.parent / ".env")` to read from root.
- Only `GEMINI_API_KEY` is strictly required for local dev. `YOUTUBE_API_KEY` and `YOUTUBE_TRANSCRIPT_DEV_API_KEY` are needed for the full experience.

## Misc

- Index-based API endpoints for recipes are fragile (race conditions if list order changes). Switched to ID-based endpoints.
- Em-dashes in markdown docs can cause rendering issues in some contexts - use hyphens instead.
