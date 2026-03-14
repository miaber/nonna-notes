import os
import json
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, Form, HTTPException, UploadFile, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google import genai
from google.genai import types
from pathlib import Path
from dotenv import load_dotenv
from gemini_session import GeminiSession, build_system_prompt, TOOLS, _read_draft, is_url, parse_recipe_via_agent, _format_structured_recipe
import pantry as pantry_store
import storage

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DEV_MODEL = "gemini-2.5-flash"
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "").strip()
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()

app = FastAPI()

# ── CORS ──────────────────────────────────────────────────────────────────────
_allowed_origin = os.getenv("ALLOWED_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_allowed_origin] if _allowed_origin != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Firebase Admin (lazy init) ─────────────────────────────────────────────────
def _init_firebase():
    """Reuse storage.py's initializer so storageBucket is always configured."""
    storage._ensure_firebase()


# ── Auth helpers ──────────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)


def _get_user_id(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Dependency: verify token and return user_id.

    - FIREBASE_PROJECT_ID set → verify Firebase ID token, return uid
    - Otherwise → check ACCESS_TOKEN (simple string), return "default"
    """
    if FIREBASE_PROJECT_ID:
        token = creds.credentials if creds else None
        if not token:
            raise HTTPException(status_code=401, detail="Missing authentication token")
        try:
            _init_firebase()
            import firebase_admin.auth as fb_auth
            decoded = fb_auth.verify_id_token(token)
            return decoded["uid"]
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    else:
        # Local dev / simple token mode
        if ACCESS_TOKEN and (not creds or creds.credentials != ACCESS_TOKEN):
            raise HTTPException(status_code=401, detail="Invalid or missing access token")
        return "default"


def _get_user_id_ws(token: str | None) -> str | None:
    """Return user_id string or None if auth fails (for WebSocket — can't raise HTTP)."""
    if FIREBASE_PROJECT_ID:
        if not token:
            return None
        try:
            _init_firebase()
            import firebase_admin.auth as fb_auth
            decoded = fb_auth.verify_id_token(token)
            return decoded["uid"]
        except Exception:
            return None
    else:
        if not ACCESS_TOKEN:
            return "default"
        return "default" if token == ACCESS_TOKEN else None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/draft")
async def get_draft(user_id: str = Depends(_get_user_id)):
    """No single draft: we have multiple drafts like any recipe. Return null; use GET /recipes to list all (including drafts)."""
    return {"draft": None}


@app.post("/dev/chat")
async def dev_chat(
    text: str = Form(""),
    recipe: str = Form(""),
    image: UploadFile = File(None),
    image_url: str = Form(""),
):
    image_bytes = None
    if image and image.filename:
        image_bytes = await image.read()
    elif image_url:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            r = await client.get(image_url)
            if r.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Could not fetch image: {r.status_code}")
            image_bytes = r.content

    recipe_text = None
    if recipe.strip():
        if is_url(recipe.strip()):
            result = await parse_recipe_via_agent(recipe.strip(), "nonna")
            if not result:
                raise HTTPException(status_code=400, detail="Could not fetch recipe (recipe-agent unavailable or failed)")
            recipe_text = _format_structured_recipe(result[0])
        else:
            recipe_text = recipe.strip()
    system_prompt = build_system_prompt(recipe_text)

    contents = []
    if image_bytes:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
    user_text = text.strip() or "Greet the user warmly and introduce yourself as Nonna. Keep it to 2-3 sentences."
    contents.append(types.Part.from_text(text=user_text))
    if not image_bytes and not text.strip() and not recipe.strip():
        raise HTTPException(status_code=400, detail="Provide at least one of: image, image_url, text, recipe")

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model=DEV_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=TOOLS,
        ),
    )

    tool_calls = []
    for candidate in response.candidates or []:
        for part in candidate.content.parts or []:
            if part.function_call:
                fc = part.function_call
                tool_calls.append({
                    "name": fc.name,
                    "args": dict(fc.args) if fc.args else {},
                })

    return {"response": response.text, "tool_calls": tool_calls}


@app.get("/pantry")
async def get_pantry(_: str = Depends(_get_user_id)):
    return {"pantry": pantry_store.load()}


@app.post("/pantry")
async def update_pantry(body: dict, _: str = Depends(_get_user_id)):
    updates = body.get("updates", [])
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided.")
    updated = pantry_store.apply_and_save(updates, allow_add=True)
    return {"pantry": updated}


@app.delete("/pantry/{name}")
async def delete_pantry_item(name: str, _: str = Depends(_get_user_id)):
    pantry = pantry_store.load()
    pantry = [p for p in pantry if p["name"].lower() != name.lower()]
    with open(pantry_store.PANTRY_FILE, "w") as f:
        json.dump(pantry, f, indent=2)
    return {"pantry": pantry}


@app.get("/pantry/recipes")
async def pantry_recipes(_: str = Depends(_get_user_id)):
    current_pantry = pantry_store.load()
    if not current_pantry:
        raise HTTPException(status_code=400, detail="Pantry is empty — add some ingredients first.")

    have = [p["name"] for p in current_pantry if p["status"] in ("have", "low")]
    out  = [p["name"] for p in current_pantry if p["status"] == "out"]

    pantry_desc = "Available: " + ", ".join(have)
    if out:
        pantry_desc += "\nOut of: " + ", ".join(out)

    prompt = f"""Based on this pantry, suggest 4 real recipes the user can cook right now.
Prefer recipes that use mostly what's available; note any small missing items.

{pantry_desc}

For each recipe, provide a direct URL to a real recipe page on a well-known site such as
allrecipes.com, budgetbytes.com, simplyrecipes.com, food.com, or seriouseats.com.
Only include URLs you are confident actually exist and point to the correct recipe.

Respond with a JSON array (no markdown, no extra text) in this exact shape:
[
  {{
    "name": "Recipe name",
    "description": "One sentence describing the dish and which pantry items it uses.",
    "url": "https://www.allrecipes.com/recipe/...",
    "missing": ["ingredient A", "ingredient B"]
  }}
]
Keep 'missing' to at most 2–3 minor items. If nothing is missing, use an empty array."""

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model=DEV_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )

    try:
        recipes = json.loads(response.text)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to parse recipe suggestions.")

    return {"recipes": recipes}


def _entries_summary(entries: list[dict]) -> list[dict]:
    """Return copies of entries with photos stripped to { step_id } only (no base64 data)."""
    out = []
    for e in entries:
        copy = {**e, "photos": [{"step_id": p.get("step_id")} for p in e.get("photos") or []]}
        out.append(copy)
    return out


@app.get("/recipes")
async def get_saved_recipes(user_id: str = Depends(_get_user_id)):
    """Return all saved recipes for this user, most recent first. Omits photo base64 to reduce memory and payload."""
    entries = storage.load_entries(user_id)
    return {"recipes": _entries_summary(entries)}


@app.get("/recipes/{recipe_id}")
async def get_recipe_by_id(recipe_id: str, user_id: str = Depends(_get_user_id)):
    """Return one recipe entry with full photo data (for detail view)."""
    entry = storage.load_entry(user_id, recipe_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return {"recipe": entry}


@app.patch("/recipes/{recipe_id}")
async def update_recipe(recipe_id: str, body: dict, user_id: str = Depends(_get_user_id)):
    entry = storage.load_entry(user_id, recipe_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Recipe not found")
    if "notes" in body:
        entry["notes"] = body["notes"]
    if "name" in body:
        entry["recipe"]["name"] = body["name"]
    recipe_patch = body.get("recipe")
    if isinstance(recipe_patch, dict):
        for key in ("name", "description", "servings", "total_time_minutes", "ingredients", "steps", "tips"):
            if key in recipe_patch:
                entry["recipe"][key] = recipe_patch[key]
    storage.save_entry(user_id, entry)
    entries = storage.load_entries(user_id)
    return {"recipes": _entries_summary(entries)}


@app.delete("/recipes/{recipe_id}")
async def delete_recipe(recipe_id: str, user_id: str = Depends(_get_user_id)):
    entry = storage.load_entry(user_id, recipe_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Recipe not found")
    storage.delete_entry(user_id, recipe_id)
    entries = storage.load_entries(user_id)
    return {"recipes": _entries_summary(entries)}


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(default=None),
):
    user_id = _get_user_id_ws(token)
    if user_id is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    session = GeminiSession(user_id=user_id)
    try:
        await session.run(websocket)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        import sys
        print(f"[websocket_endpoint] {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        await session.close()
