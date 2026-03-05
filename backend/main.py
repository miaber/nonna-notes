import os
import json
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from dotenv import load_dotenv
from gemini_session import GeminiSession, build_system_prompt, TOOLS, _read_draft, _load_recipe_entries, _save_recipe_entries
from recipe_fetcher import fetch_recipe, is_url
import pantry as pantry_store

load_dotenv()

DEV_MODEL = "gemini-2.5-flash"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before submission
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/draft")
async def get_draft():
    """Return the current document draft if any (name, steps, ingredients — no photo blobs)."""
    entry = _read_draft()
    if not entry:
        return {"draft": None}
    r = entry.get("recipe") or {}
    return {
        "draft": {
            "name": r.get("name"),
            "steps": r.get("steps", []),
            "ingredients": r.get("ingredients", []),
            "updated_at": entry.get("updated_at"),
            "started_at": entry.get("started_at"),
        }
    }


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
            try:
                recipe_text = await fetch_recipe(recipe.strip())
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Could not fetch recipe: {e}")
        else:
            recipe_text = recipe.strip()
    system_prompt = build_system_prompt(recipe_text)

    contents = []
    if image_bytes:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
    # Use provided text, or the same greeting trigger the live session uses on startup
    user_text = text.strip() or "Greet the user warmly and introduce yourself as Mise. Keep it to 2-3 sentences."
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
async def get_pantry():
    return {"pantry": pantry_store.load()}


@app.post("/pantry")
async def update_pantry(body: dict):
    """Add or update pantry items. Body: {"updates": [{name, status}]}"""
    updates = body.get("updates", [])
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided.")
    updated = pantry_store.apply_and_save(updates, allow_add=True)
    return {"pantry": updated}


@app.delete("/pantry/{name}")
async def delete_pantry_item(name: str):
    pantry = pantry_store.load()
    pantry = [p for p in pantry if p["name"].lower() != name.lower()]
    with open(pantry_store.PANTRY_FILE, "w") as f:
        json.dump(pantry, f, indent=2)
    return {"pantry": pantry}


@app.get("/pantry/recipes")
async def pantry_recipes():
    """Suggest recipes the user can make from their current pantry."""
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


def _load_all_recipes() -> list[dict]:
    from gemini_session import SAVED_RECIPES_PATH
    entries = []
    try:
        with open(SAVED_RECIPES_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    if "photos" not in entry:
                        entry["photos"] = []
                    entries.append(entry)
    except FileNotFoundError:
        pass
    return entries


def _save_all_recipes(entries: list[dict]) -> None:
    from gemini_session import SAVED_RECIPES_PATH
    with open(SAVED_RECIPES_PATH, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


@app.get("/recipes")
async def get_saved_recipes():
    """Return all saved recipes, most recent first."""
    entries = _load_all_recipes()
    entries.reverse()
    return {"recipes": entries}


@app.patch("/recipes/{index}")
async def update_recipe(index: int, body: dict):
    """
    Update a saved recipe by index (0 = most recent).
    Accepted fields: notes (str), name (str), recipe (dict with any of:
    name, description, servings, total_time_minutes, ingredients, steps, tips).
    """
    entries = _load_all_recipes()
    entries.reverse()  # 0 = most recent, matches frontend
    if index < 0 or index >= len(entries):
        raise HTTPException(status_code=404, detail="Recipe not found")
    entry = entries[index]
    if "notes" in body:
        entry["notes"] = body["notes"]
    if "name" in body:
        entry["recipe"]["name"] = body["name"]
    recipe_patch = body.get("recipe")
    if isinstance(recipe_patch, dict):
        for key in ("name", "description", "servings", "total_time_minutes", "ingredients", "steps", "tips"):
            if key in recipe_patch:
                entry["recipe"][key] = recipe_patch[key]
    entries.reverse()  # back to chronological order for storage
    _save_all_recipes(entries)
    entries.reverse()  # return most-recent-first
    return {"recipes": entries}


@app.delete("/recipes/{index}")
async def delete_recipe(index: int):
    """Remove a saved recipe by index (0 = most recent)."""
    entries = _load_all_recipes()
    entries.reverse()  # 0 = most recent, matches frontend
    if index < 0 or index >= len(entries):
        raise HTTPException(status_code=404, detail="Recipe not found")
    entries.pop(index)
    entries.reverse()  # back to chronological order for storage
    _save_all_recipes(entries)
    entries.reverse()
    return {"recipes": entries}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session = GeminiSession()
    try:
        await session.run(websocket)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        import sys
        print(f"[websocket_endpoint] {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        await session.close()
