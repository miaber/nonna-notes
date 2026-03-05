import asyncio
import json
import base64
import os
import time
import warnings
from datetime import datetime, timezone
from dotenv import load_dotenv
import httpx
from google import genai
from google.genai import types
from starlette.websockets import WebSocketDisconnect
from recipe_fetcher import fetch_recipe, is_url

load_dotenv()

warnings.filterwarnings("ignore", message=".*non-data parts.*")


MODEL = "gemini-2.5-flash-native-audio-preview-09-2025"
RECIPE_AGENT_URL = os.getenv("RECIPE_AGENT_URL", "http://localhost:8001")
SAVED_RECIPES_PATH = os.path.join(os.path.dirname(__file__), "saved_recipes.json")


def _sanitize_transcript(text: str) -> str:
    """Strip control characters and ctrl46-style output; return empty if nothing meaningful left. Preserve spaces."""
    if not text or not isinstance(text, str):
        return ""
    # Remove control characters (ASCII 0-31, 127) only; keep spaces (0x20)
    cleaned = "".join(c for c in text if ord(c) >= 32 and ord(c) != 127)
    # Skip if it looks like repeated control-character placeholders (e.g. <ctrl46>)
    stripped = cleaned.strip()
    if stripped and "<ctrl" in stripped.lower() and stripped.replace("<ctrl46>", "").replace("<ctrl46", "").strip() == "":
        return ""
    # Return cleaned without stripping so space-only chunks are not dropped (avoids words running together)
    return cleaned


def _deep_convert(obj):
    """Recursively convert proto/mapping objects to plain Python dicts/lists."""
    if isinstance(obj, dict):
        return {k: _deep_convert(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_deep_convert(v) for v in obj]
    if hasattr(obj, "items"):  # proto Struct / MapComposite
        return {k: _deep_convert(v) for k, v in obj.items()}
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
        return [_deep_convert(v) for v in obj]
    return obj

# ── Recipe helpers ───────────────────────────────────────────────────────────

def _format_structured_recipe(r: dict) -> str:
    """Convert structured recipe JSON into clean prompt text for Gemini."""
    lines = [f"# {r.get('name', 'Recipe')}", ""]
    if r.get("description"):
        lines += [r["description"], ""]
    if r.get("servings"):
        lines.append(f"Serves: {r['servings']}")
    if r.get("total_time_minutes"):
        lines.append(f"Total time: {r['total_time_minutes']} minutes")
    lines += ["", "Ingredients:"]
    for ing in r.get("ingredients", []):
        prep = f", {ing['prep']}" if ing.get("prep") else ""
        lines.append(f"  - {ing.get('amount', '')} {ing.get('item', '')}{prep}")
    lines += ["", "Steps:"]
    for step in r.get("steps", []):
        timer = f" [{step['timer_seconds']}s]" if step.get("timer_seconds") else ""
        visual = " [visual check]" if step.get("visual_checkpoint") else ""
        lines.append(f"  {step['id']}. {step['instruction']}{timer}{visual}")
    if r.get("tips"):
        lines += ["", "Tips:"] + [f"  - {t}" for t in r["tips"]]
    return "\n".join(lines)


def _build_live_recipe(
    name: str,
    steps: list[dict],
    session_start_ts: float | None = None,
    ingredients: list[dict] | None = None,
    **kwargs: object,
) -> dict:
    """Convert observed live steps and ingredients into a RecipeSchema-compatible dict."""
    built_steps = [
        {
            "id": i + 1,
            "instruction": s.get("instruction", ""),
            "timer_seconds": s.get("timer_seconds"),
            "visual_checkpoint": False,
        }
        for i, s in enumerate(steps)
    ]
    built_ingredients = []
    for ing in ingredients or []:
        built_ingredients.append({
            "amount": ing.get("amount") or "",
            "item": (ing.get("item") or "").strip(),
            "prep": ing.get("prep") or "",
        })
    # Documented recipe: overall time for the recipe book (single session or accumulated across draft resumes)
    total_elapsed_seconds: float | None = kwargs.get("total_elapsed_seconds")
    if total_elapsed_seconds is not None:
        total_time_minutes = max(1, round(total_elapsed_seconds / 60))
    elif session_start_ts is not None:
        total_time_minutes = max(1, round((time.time() - session_start_ts) / 60))
    else:
        total_seconds = sum(s.get("timer_seconds") or 0 for s in steps)
        total_time_minutes = round(total_seconds / 60) if total_seconds else None
    return {
        "name": name,
        "description": "Documented live by Mise while you cooked.",
        "servings": 2,
        "total_time_minutes": total_time_minutes,
        "ingredients": built_ingredients,
        "steps": built_steps,
        "tips": [],
    }


async def _youtube_search(query: str) -> str | None:
    """Search YouTube Data API v3 for a video matching query. Returns videoId or None if unavailable."""
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as hx:
            resp = await hx.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "snippet",
                    "type": "video",
                    "videoCategoryId": "10",  # Music
                    "q": query,
                    "key": api_key,
                    "maxResults": 1,
                },
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if items:
                return items[0]["id"]["videoId"]
    except Exception as e:
        print(f"[mise] youtube search failed: {e}", flush=True)
    return None


def _save_recipe_locally(recipe: dict, photos: list[dict] | None = None) -> None:
    """Append a recipe to saved_recipes.json (newline-delimited JSON). photos: [{"step_id": int, "data": "base64..."}]."""
    photo_list = photos or []
    entry = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "recipe": recipe,
        "photos": photo_list,
    }
    try:
        with open(SAVED_RECIPES_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
        print(f"[mise] recipe saved: {recipe.get('name', '?')} ({len(photo_list)} photos)", flush=True)
    except Exception as e:
        print(f"[mise] save recipe failed: {e}", flush=True)


def _load_recipe_entries() -> list[dict]:
    """Load all entries from saved_recipes (including draft). Each entry has recipe, photos, and optionally draft, saved_at."""
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


def _save_recipe_entries(entries: list[dict]) -> None:
    """Write all entries to saved_recipes (newline-delimited JSON)."""
    with open(SAVED_RECIPES_PATH, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _read_draft(started_at: float | None = None) -> dict | None:
    """Return a draft entry. If started_at is given, return that specific draft; otherwise return the most recent one."""
    try:
        entries = _load_recipe_entries()
        drafts = [e for e in entries if e.get("draft") and e.get("recipe")]
        if not drafts:
            return None
        if started_at is not None:
            for e in drafts:
                if e.get("started_at") == started_at:
                    return e
            return None
        # Most recent draft (last in file = most recent append)
        return drafts[-1]
    except Exception as e:
        print(f"[mise] read draft failed: {e}", flush=True)
    return None


def _write_draft(
    live_steps: list[dict],
    live_ingredients: list[dict],
    name: str | None = None,
    started_at: float | None = None,
    photos: list[dict] | None = None,
    accumulated_seconds: float | None = None,
) -> None:
    """Write draft as a recipe-shaped entry with draft: true. Photos and accumulated_seconds persist when resuming."""
    try:
        recipe = _build_live_recipe(
            name or "Draft",
            live_steps,
            # Use accumulated_seconds when available (correct for multi-session cooks).
            # Fall back to session_start_ts only for fresh single-session drafts.
            session_start_ts=started_at if accumulated_seconds is None else None,
            ingredients=live_ingredients,
            total_elapsed_seconds=accumulated_seconds,
        )
        entry = {
            "draft": True,
            "recipe": recipe,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "photos": photos or [],
        }
        if started_at is not None:
            entry["started_at"] = started_at
        if accumulated_seconds is not None:
            entry["accumulated_seconds"] = accumulated_seconds
        # Replace only the draft for THIS session (same started_at); keep other drafts untouched
        entries = _load_recipe_entries()
        entries = [e for e in entries if not (e.get("draft") and e.get("started_at") == (started_at or entry.get("started_at")))]
        entries.append(entry)
        _save_recipe_entries(entries)
    except Exception as e:
        print(f"[mise] write draft failed: {e}", flush=True)


def _clear_draft() -> None:
    """Remove the draft entry from saved recipes (draft: true)."""
    try:
        entries = [e for e in _load_recipe_entries() if not e.get("draft")]
        if len(entries) != len(_load_recipe_entries()):
            _save_recipe_entries(entries)
    except Exception as e:
        print(f"[mise] clear draft failed: {e}", flush=True)


# ── Persona prompts ───────────────────────────────────────────────────────────

# Shared by both personas; only personality differs below.
COMMON_BASE = """\
You are in a voice session: the user hears you only when you speak. Every time the user says something, you MUST reply out loud with spoken audio. Never respond with only internal thought — always speak your response so the user can hear it.
Keep responses SHORT — 1-2 sentences unless giving step-by-step instructions. Never repeat yourself. Never ask the same question twice in a row. Greet the user only once at the start of the session.
TOOL CALLS ARE THE ONLY WAY ACTIONS HAPPEN. Never say you did something (saved, edited, recorded, added, started) without actually calling the corresponding tool in that same response. Saying it without calling the tool does nothing.
Take photos proactively — no need to ask. Never tell the user to adjust the camera or ask for a better angle.
If the user asks to play music or put something on, call play_music(query) with a descriptive YouTube search query. If they ask to stop or turn off the music, call stop_music(). If they ask to lower, raise, or set the volume, call set_music_volume(volume) with a value 0–100."""

GORDON_PERSONALITY = """\
You are Mise, a savage British chef in the style of Gordon Ramsay at his most brutal.
Impatient, sharp-tongued, appalled by mediocrity. Use real Ramsay-style insults:
"This is a disaster.", "Bloody hell.", "What IS that?", "You donkey!", "It's DRY.", "Disgusting."
Short. Brutal. No softening. You acknowledge good work only briefly and grudgingly.
When you see food through the camera, tear it apart first, then explain how to fix it."""

NONNA_PERSONALITY = """\
You are Nonna, a dramatic Italian grandmother who has cooked since 1974.
LANGUAGE RULE: Speak in English sentences — never a full sentence in Italian. But sprinkle Italian words and phrases freely throughout your English for flavour: allora, dai, mamma mia, certo, uffa, bene, Madonna, bellissimo, andiamo, prego, che disastro, bravo, coraggio, vabbè, guarda — use them often as exclamations, transitions, and asides. The more Italian seasoning, the better — just keep the actual sentences in English.
Give your English a thick Italian accent flavour: drop articles ("Is very important!"), add "-a" to words ("you must-a stir!", "is-a no good"), use third person ("Nonna would never!", "Nonna is watching you!"), address the user as "cara" or "tesoro". Reference your village in Calabria and your mother's wooden spoon. Be warm and loving but deeply offended by bad technique.
When you see food through the camera, gasp dramatically, then guide lovingly."""

PERSONAS = {
    "gordon": {"base": GORDON_PERSONALITY + "\n\n" + COMMON_BASE, "voice": "Algieba"},
    "nonna":  {"base": NONNA_PERSONALITY + "\n\n" + COMMON_BASE,  "voice": "Kore"},
}

# ── Tools ────────────────────────────────────────────────────────────────────
TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="set_timer",
                description="Start a countdown timer. Recipe mode only — call when the user says to start timing a step. Not for document mode.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "label": types.Schema(type="STRING"),
                        "duration_seconds": types.Schema(type="INTEGER"),
                    },
                    required=["label", "duration_seconds"],
                ),
            ),
            types.FunctionDeclaration(
                name="start_stopwatch",
                description="Document mode: start an elapsed-time stopwatch when the cook begins a timed step (e.g. 'browning for 2 minutes'). When they finish, call add_live_step without timer_seconds — the app fills elapsed time. Only for explicitly timed steps.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "label": types.Schema(type="STRING", description="Short label for the step, e.g. 'Browning', 'Pasta boiling'"),
                    },
                    required=["label"],
                ),
            ),
            types.FunctionDeclaration(
                name="complete_step",
                description="Mark a recipe step as done.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "step_number": types.Schema(type="INTEGER"),
                    },
                    required=["step_number"],
                ),
            ),
            types.FunctionDeclaration(
                name="fetch_recipe",
                description="Look up and display a recipe. ONLY way to provide a recipe — never recite one yourself. Ask 2+ clarifying questions first and wait for answers before calling.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "dish_name": types.Schema(type="STRING", description="Name or description of the dish"),
                    },
                    required=["dish_name"],
                ),
            ),
            types.FunctionDeclaration(
                name="add_live_step",
                description="Document mode: record a cooking step. Call this in the same turn whenever you narrate a step — if you say it, you must call this. Saying the step without calling the tool does not record it. Ask what they're doing only if genuinely unclear. Include timer_seconds only when they stated a duration.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "instruction": types.Schema(type="STRING", description="What the cook just did"),
                        "timer_seconds": types.Schema(type="INTEGER", description="Optional. Only when the user explicitly said a duration for this step (e.g. 30 for '30 seconds', 300 for '5 minutes'). Omit for steps with no stated time; omit when you used start_stopwatch (app fills elapsed)."),
                    },
                    required=["instruction"],
                ),
            ),
            types.FunctionDeclaration(
                name="delete_live_step",
                description="Document mode: remove a recorded step by its 1-based number. Use when the user says a step was wrong, didn't happen, or shouldn't be recorded. Always prefer delete+re-add (or edit_live_step) over keeping a wrong step.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "step_number": types.Schema(type="INTEGER", description="1-based index of the step to remove"),
                    },
                    required=["step_number"],
                ),
            ),
            types.FunctionDeclaration(
                name="edit_live_step",
                description="Document mode: correct an existing step in place. Use when the user says the wording is wrong or wants to refine a step that was already recorded.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "step_number": types.Schema(type="INTEGER", description="1-based index of the step to update"),
                        "instruction": types.Schema(type="STRING", description="The corrected instruction text"),
                        "timer_seconds": types.Schema(type="INTEGER", description="Updated timer, or omit to keep existing value"),
                    },
                    required=["step_number", "instruction"],
                ),
            ),
            types.FunctionDeclaration(
                name="add_live_ingredient",
                description="Document mode: add ONE ingredient. Call once per ingredient — 4 ingredients = 4 calls. If no amount given, ask 'How much?' first then call with complete info. Saying it in speech does NOT add it; only this tool does.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "amount": types.Schema(type="STRING", description="Quantity or measure, e.g. '2', '1 cup', '½ cup', 'a pinch'. Use empty string or omit if unknown and they didn't specify."),
                        "item": types.Schema(type="STRING", description="Ingredient name, e.g. 'eggs', 'all-purpose flour', 'olive oil'"),
                        "prep": types.Schema(type="STRING", description="Optional prep note, e.g. 'diced', 'at room temperature', 'packed'. Empty string if none."),
                    },
                    required=["item"],
                ),
            ),
            types.FunctionDeclaration(
                name="set_draft_name",
                description="Document mode: call when the user names the recipe they're making.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "name": types.Schema(type="STRING", description="The recipe name the user said"),
                    },
                    required=["name"],
                ),
            ),
            types.FunctionDeclaration(
                name="finalize_live_recipe",
                description="Document mode: call ONCE when the cook says they're finished or the meal is complete. This builds the recipe, shows it on screen, and saves it to My Recipes automatically. Do NOT call it again for the same cook.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "name": types.Schema(type="STRING", description="A descriptive name for what was cooked"),
                    },
                    required=["name"],
                ),
            ),
            types.FunctionDeclaration(
                name="save_recipe_to_library",
                description="Save the current recipe to My Recipes. Call when the user asks to save or keep it. Saying 'saved' without calling this tool does nothing.",
                parameters=types.Schema(type="OBJECT", properties={}),
            ),
            types.FunctionDeclaration(
                name="capture_step_photo",
                description="Save a photo from the camera. Call immediately when user asks to take a photo — not calling this does NOT save it. Also take photos at end of steps and when plating. Skip if only a recipe name exists (no steps yet). Use step_number only for steps that exist; omit for on-demand shots.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "step_number": types.Schema(type="INTEGER", description="Optional. Recipe step number (1-based) when this photo is for a specific step; omit for general on-demand photos."),
                    },
                    required=[],
                ),
            ),
            types.FunctionDeclaration(
                name="play_music",
                description="Play background music on the user's screen. Call this when the user asks to play music, put on some background music, or requests a specific style of music (e.g. 'play some Italian music', 'put on jazz', 'play something relaxing'). Provide a descriptive YouTube search query.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "query": types.Schema(type="STRING", description="YouTube search query describing the music, e.g. 'Italian folk music cooking playlist', 'relaxing jazz cooking', 'classical piano background music'"),
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="stop_music",
                description="Stop the background music that is currently playing. Call this when the user asks to stop, pause, or turn off the music.",
                parameters=types.Schema(type="OBJECT", properties={}),
            ),
            types.FunctionDeclaration(
                name="edit_ingredient",
                description="Change an ingredient in the current recipe (e.g. swap swiss for cheddar, update amount). Index is 0-based from the ingredient list. Pass all fields.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "index": types.Schema(type="INTEGER", description="0-based position of the ingredient in the ingredients list"),
                        "amount": types.Schema(type="STRING", description="Amount/quantity, e.g. '200g', '2 cups'. Empty string if none."),
                        "item": types.Schema(type="STRING", description="Ingredient name, e.g. 'cheddar cheese'"),
                        "prep": types.Schema(type="STRING", description="Prep note, e.g. 'grated'. Empty string if none."),
                    },
                    required=["index", "item"],
                ),
            ),
            types.FunctionDeclaration(
                name="edit_step",
                description="Change a step in the current recipe. Step id is 1-based.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "step_number": types.Schema(type="INTEGER", description="Step id (1-based) to update"),
                        "instruction": types.Schema(type="STRING", description="Full updated instruction for this step"),
                        "timer_seconds": types.Schema(type="INTEGER", description="Updated timer in seconds, or null if the step has no timer"),
                    },
                    required=["step_number", "instruction"],
                ),
            ),
            types.FunctionDeclaration(
                name="set_music_volume",
                description="Set the volume of the background music. Call this when the user asks to make the music louder, quieter, lower, higher, etc. Volume is 0 (silent) to 100 (full).",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "volume": types.Schema(type="INTEGER", description="Volume level 0–100. Use ~20 for quiet/background, ~50 for medium, ~80 for loud."),
                    },
                    required=["volume"],
                ),
            ),
        ]
    )
]


def _build_system_prompt(persona: str, recipe_text: str | None) -> str:
    p = PERSONAS.get(persona, PERSONAS["gordon"])
    parts = [p["base"]]

    if recipe_text:
        parts.append(
            "\nA recipe has already been parsed and displayed on screen before this session started."
            "\nGreet the user, confirm the recipe, and ask if they are ready to begin."
            "\nWalk them through it step by step. Call complete_step(step_number) as each step is done."
            "\nCall set_timer ONLY when the user explicitly says to start timing."
            "\nWhen the user asks you to take a photo, you MUST call capture_step_photo — that is the only way a photo is saved. Also take photos proactively: at key moments a cook might want to look back on (after prep, mid-cook, plating, finished dish, any visually interesting state). When in doubt, take the photo — more is better than missing a moment."
            "\nIf the user asks to save this recipe or add it to My Recipes, you MUST call save_recipe_to_library — that is the only way it gets saved; do not just say it's saved."
            "\nIf the user asks to change an ingredient (e.g. 'use cheddar instead of swiss', 'make it 2 cups'), call edit_ingredient(index, amount, item, prep) — the index is 0-based from the ingredient list above. If they ask to change a step (e.g. 'bake for 25 minutes instead'), call edit_step(step_number, instruction, timer_seconds). NEVER say you made a change without actually calling the tool — saying it does not update the recipe on screen; only the tool call does."
            "\nAfter calling edit_ingredient or edit_step: confirm the change in one sentence and STOP. Do not continue with the recipe or ask further questions — wait for the user to speak first."
        )
        parts.append(f"\n--- RECIPE ---\n{recipe_text}\n--- END RECIPE ---")
    else:
        parts.append(
            "\nNo recipe is loaded. Your ONLY job right now is to find the right recipe through conversation."
            "\n\nYou MUST use the fetch_recipe tool to load any recipe — NEVER suggest, describe, or recite a recipe from your own knowledge. Every dish must come from fetch_recipe."
            "\n\nSTRICT CONVERSATION FLOW:"
            "\n  Step 1 — Greet and ask what they want to cook. One short question only."
            "\n  Step 2 — Once they name a dish, ask 2–3 clarifying questions — whatever you actually need to know to find the right version of that specific dish. Ask them all in one turn, naturally."
            "\n  Step 3 — Only AFTER the user answers, call fetch_recipe with a specific refined query"
            " (e.g. 'quick weeknight carbonara' or 'vegetarian Thai green curry 30 minutes')."
            "\n  Step 4 — After fetch_recipe returns, briefly read the recipe name and key details, then ask:"
            " 'Does that sound good, or shall I find something different?' If they want another, ask a follow-up and call fetch_recipe again."
            "\n\nNever skip Step 2. Naming the dish is NOT the same as answering the clarifying questions — you must ask them explicitly."
"\n\nIf the user says they're free-cooking, experimenting, or just winging it: enter document mode."
            "\n\nIMPORTANT — ambiguous cooking statements: if the user says something like 'we're making X today', 'I'm cooking X', 'let's make X', or names a dish WITHOUT asking for a recipe, do NOT automatically search for a recipe. Instead ask: 'Are you following a recipe, or shall I watch and document what you make?' Wait for their answer before doing anything."
            "\n\nDOCUMENT MODE rules — follow these exactly:"
            "\n• INGREDIENTS: When they mention an ingredient with an amount, call add_live_ingredient immediately in the SAME turn. If no amount given, ask 'How much?' first, then call once you have the answer. NEVER say 'adding X to the list' or 'I'll add X' without having called add_live_ingredient in that same turn — saying it does not add it."
            "\n• STEPS: Call add_live_step whenever you describe or narrate a step the cook is doing — whether they say it, you see it on camera and say it back to them, or both. The key rule: if you narrate a step in your speech ('now you pour the water', 'Nonna sees you chopping'), you MUST also call add_live_step for it. Do NOT narrate steps you have not recorded. The only time to ask is if you genuinely cannot tell what they are doing at all."
            "\n• CORRECTING STEPS: When the user says a step was wrong, didn't happen, or needs changing: ALWAYS use delete_live_step or edit_live_step immediately — do NOT just add a new step on top of the wrong one. edit_live_step is preferred when the instruction only needs minor correction. delete_live_step is for steps that simply should not have been recorded. Step numbers are shown on screen (1-based). If the user says 'that last step was wrong', the step to fix is step number equal to the current count."
            "\n• TOOL FIRST: For both ingredients and steps, call the tool BEFORE or DURING the same spoken turn. Never describe an action you are taking without the tool call happening in the same response."
            "\n• NAMES: When they name the recipe, call set_draft_name."
            "\n• PHOTOS: Take photos at key moments — after prep, mid-cook, plating, any state worth remembering. When in doubt, take it. Only use step_number values that exist in the current recipe."
            "\n• TIMERS: NEVER call set_timer in document mode. If the user mentions a duration ('boil for 5 minutes'), record it in add_live_step via timer_seconds. Do not start an actual countdown — just document the time."
            "\n• FINISH: When done, call finalize_live_recipe(name) ONCE."
        )

    return "\n".join(parts)


def build_system_prompt(recipe_text: str | None, persona: str = "gordon") -> str:
    """Used by /dev/chat endpoint."""
    return _build_system_prompt(persona, recipe_text)


# Singleton client — reuses the underlying HTTP/gRPC transport across sessions
_gemini_client: genai.Client | None = None

def _get_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _gemini_client


class GeminiSession:
    def __init__(self):
        self.client = _get_client()
        self.current_recipe: dict | None = None
        self.live_steps: list[dict] = []
        self.live_ingredients: list[dict] = []
        self.draft_name: str | None = None  # set when user gives recipe a name (set_draft_name)
        self.draft_accumulated_seconds: float = 0.0  # total time spent on this draft across sessions (restored on resume)
        self.persona: str = "gordon"
        self.last_video_frame: str | None = None  # base64
        self.step_photos: list[dict] = []  # [{"step_id": int, "data": "base64..."}]
        self._saved_recipe_name_this_session: str | None = None  # avoid duplicate saves when model calls save_recipe_to_library multiple times
        self.active_stopwatch_label: str | None = None  # document mode: label of current count-up timer
        self.document_mode_started_at: float | None = None  # start of THIS session (reset to time.time() on every resume/start)
        self._draft_key: float | None = None  # original started_at used to identify the draft entry in saved_recipes.json (never changes on resume)

    async def run(self, websocket):
        raw = await websocket.receive_text()
        config_msg = json.loads(raw)

        self.persona = config_msg.get("persona", "gordon")
        persona = self.persona
        p = PERSONAS.get(persona, PERSONAS["gordon"])
        print(f"[mise] persona={persona}  voice={p['voice']}", flush=True)

        recipe_text = await self._resolve_recipe(config_msg)

        if recipe_text:
            print(f"[mise] recipe: {len(recipe_text)} chars", flush=True)

        system_prompt = _build_system_prompt(persona, recipe_text)
        print(f"[mise] system prompt: {len(system_prompt)} chars", flush=True)

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription=types.AudioTranscriptionConfig(),
            system_instruction=system_prompt,
            tools=TOOLS,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=p["voice"],
                    )
                )
            ),
            realtime_input_config=types.RealtimeInputConfig(
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            ),
        )

        # Retry on transient errors
        CONNECT_TIMEOUT = 20  # seconds to wait for live.connect() before retrying
        for attempt in range(5):
            try:
                t0 = time.time()
                print(f"[mise] connecting (attempt {attempt+1})…", flush=True)
                _cm = self.client.aio.live.connect(model=MODEL, config=live_config)
                try:
                    session = await asyncio.wait_for(_cm.__aenter__(), timeout=CONNECT_TIMEOUT)
                except asyncio.TimeoutError:
                    try:
                        await _cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                    print(f"[mise] connect timed out after {CONNECT_TIMEOUT}s", flush=True)
                    raise RuntimeError(f"live.connect() timed out after {CONNECT_TIMEOUT}s")
                print(f"[mise] connected ✓ ({time.time()-t0:.1f}s)", flush=True)
                try:

                    # Drain buffered browser frames
                    while True:
                        try:
                            await asyncio.wait_for(websocket.receive_text(), timeout=0.05)
                        except asyncio.TimeoutError:
                            break

                    # Push structured recipe to frontend before Gemini speaks
                    recipe_json = config_msg.get("recipe_json")
                    if recipe_json:
                        await websocket.send_text(json.dumps({
                            "type": "recipe",
                            "recipe": recipe_json,
                            "source": config_msg.get("recipe_source", "unknown"),
                        }))

                    # Resume document draft if requested
                    if config_msg.get("resume_draft"):
                        draft_started_at = config_msg.get("resume_draft_started_at")
                        draft_entry = _read_draft(started_at=draft_started_at)
                        if draft_entry:
                            r = draft_entry.get("recipe") or {}
                            self.live_steps = r.get("steps") or []
                            self.live_ingredients = r.get("ingredients") or []
                            self.draft_name = r.get("name")
                            self.step_photos = draft_entry.get("photos") or []
                            self.draft_accumulated_seconds = float(draft_entry.get("accumulated_seconds") or 0)
                            # _draft_key identifies which entry to overwrite in saved_recipes.json — stays as original started_at
                            self._draft_key = draft_entry.get("started_at")
                            # document_mode_started_at tracks THIS session's clock — always reset to now on resume
                            self.document_mode_started_at = time.time()
                            await websocket.send_text(json.dumps({
                                "type": "draft_loaded",
                                "steps": self.live_steps,
                                "ingredients": self.live_ingredients,
                                "name": self.draft_name,
                            }))
                            print(f"[mise] draft loaded: {len(self.live_steps)} steps, {len(self.live_ingredients)} ingredients, {len(self.step_photos)} photos", flush=True)

                    # Kick off the conversation
                    recipe_hint = config_msg.get("recipe_hint", "").strip()
                    if config_msg.get("resume_draft") and (self.live_steps or self.live_ingredients):
                        parts = ["The user is continuing a draft recipe (already visible on screen)."]
                        if self.draft_name:
                            parts.append(f"Recipe name: {self.draft_name}.")
                        if self.live_steps:
                            parts.append("Steps so far: " + "; ".join(f"{i+1}. {s.get('instruction', '')}" for i, s in enumerate(self.live_steps)))
                        if self.live_ingredients:
                            ing_strs = [f"{i.get('amount', '')} {i.get('item', '')}".strip() or i.get("item", "") for i in self.live_ingredients]
                            parts.append("Ingredients so far: " + "; ".join(ing_strs))
                        parts.append("Greet them briefly and continue documenting. Add only NEW steps and ingredients with the tools; do not re-add what is already listed.")
                        trigger = " ".join(parts)
                    elif recipe_text:
                        trigger = "The recipe is already displayed on screen. Greet the user and check if they're ready to begin."
                    elif recipe_hint:
                        trigger = f'The user said they want to make something like "{recipe_hint}" but no recipe is loaded. Greet them and ask whatever clarifying questions you need to find the right version. Do NOT call fetch_recipe yet — wait for their answers first.'
                    else:
                        trigger = "No recipe is loaded. Greet the user briefly and ask what they'd like to cook. Once they name a dish, ask whatever you need to know to find the right recipe — then call fetch_recipe. Never recite a recipe yourself."
                    await session.send_realtime_input(text=trigger)
                    print(f"[mise] trigger sent ✓", flush=True)

                    # Counters for logging: confirm user audio is reaching the session and debug 0-audio turns
                    self._audio_chunks_sent_to_session = 0
                    self._turns_completed = 0
                    self._audio_chunks_since_last_turn = 0

                    recv_task = asyncio.create_task(self._receive_from_browser(websocket, session))
                    send_task = asyncio.create_task(self._send_to_browser(websocket, session))
                    try:
                        await asyncio.gather(recv_task, send_task)
                    finally:
                        # Cancel both so they don't touch the session after disconnect (avoids "Cannot call receive once a disconnect")
                        recv_task.cancel()
                        send_task.cancel()
                        try:
                            await asyncio.gather(recv_task, send_task)
                        except asyncio.CancelledError:
                            pass
                finally:
                    # Always close the Live API context manager
                    try:
                        await _cm.__aexit__(None, None, None)
                    except Exception:
                        pass

                break
            except Exception as e:
                err = str(e)
                print(f"[mise] FAIL: {type(e).__name__}: {err[:120]}", flush=True)
                # 1008/1007/1011/409 = known intermittent Live API errors; retry on transient
                if ("1008" in err or "1007" in err or "1011" in err or "409" in err or "disconnect" in err.lower() or "timed out" in err.lower()) and attempt < 4:
                    delay = 3 + attempt * 2
                    print(f"[mise] retrying in {delay}s…", flush=True)
                    try:
                        await websocket.send_text(json.dumps({"type": "reset"}))
                        await websocket.send_text(json.dumps({
                            "type": "transcript",
                            "text": f"[Reconnecting… ({attempt+1}/5)]\n",
                        }))
                    except Exception:
                        pass
                    await asyncio.sleep(delay)
                    continue
                raise

    async def _resolve_recipe(self, config_msg: dict) -> str | None:
        # Priority: pre-parsed structured JSON from Recipe Agent (Mode A)
        recipe_json = config_msg.get("recipe_json")
        if recipe_json:
            self.current_recipe = recipe_json
            return _format_structured_recipe(recipe_json)

        # Fallback: raw text or URL (dev/chat endpoint or Recipe Agent unavailable)
        recipe_input = (config_msg.get("recipe") or "").strip()
        if not recipe_input:
            return None
        if is_url(recipe_input):
            try:
                return await fetch_recipe(recipe_input)
            except Exception as e:
                print(f"[mise] recipe fetch failed: {e}", flush=True)
                return None
        return recipe_input

    async def _receive_from_browser(self, websocket, session):
        """Forward audio/video from browser → Gemini."""
        audio_in_count = 0
        video_in_count = 0
        try:
            async for message in websocket.iter_text():
                data = json.loads(message)
                t = data.get("type")

                if t == "audio":
                    try:
                        audio_bytes = base64.b64decode(data.get("data") or "")
                    except Exception:
                        continue
                    # PCM 16kHz 16-bit mono = even number of bytes; skip empty or odd-sized to avoid 1007 invalid payload
                    if audio_bytes and len(audio_bytes) % 2 == 0:
                        audio_in_count += 1
                        self._audio_chunks_sent_to_session = getattr(self, "_audio_chunks_sent_to_session", 0) + 1
                        self._audio_chunks_since_last_turn = getattr(self, "_audio_chunks_since_last_turn", 0) + 1
                        if audio_in_count == 1:
                            print("[mise] ← first audio from browser ✓ (forwarding to session)", flush=True)
                        elif audio_in_count % 500 == 0:
                            print(f"[mise] ← audio from browser: {audio_in_count} chunks so far", flush=True)
                        # Log first user audio in a new turn (after at least one turn_complete) so we know they're speaking
                        if getattr(self, "_turns_completed", 0) > 0 and self._audio_chunks_since_last_turn == 1:
                            print(f"[mise] user speaking (first chunk of new turn #{self._turns_completed + 1} → session)", flush=True)
                        if audio_in_count > 0 and audio_in_count % 200 == 0:
                            print(f"[mise] user audio → session: {self._audio_chunks_sent_to_session} total, {self._audio_chunks_since_last_turn} this turn", flush=True)
                        await session.send_realtime_input(
                            audio=types.Blob(data=audio_bytes, mime_type="audio/pcm;rate=16000")
                        )
                elif t == "video":
                    try:
                        video_bytes = base64.b64decode(data.get("data") or "")
                    except Exception:
                        continue
                    if not video_bytes or len(video_bytes) > 10 * 1024 * 1024:  # skip empty or >10MB
                        continue
                    video_in_count += 1
                    if video_in_count == 1:
                        print("[mise] ← first video from browser ✓", flush=True)
                    self.last_video_frame = data.get("data")
                    await session.send_realtime_input(
                        video=types.Blob(data=video_bytes, mime_type="image/jpeg")
                    )
                elif t == "text":
                    print(f"[mise] ← text from browser: {data.get('text', '')[:80]}", flush=True)
                    await session.send_realtime_input(text=data.get("text", ""))
                elif t == "stopwatch_elapsed":
                    sec = data.get("seconds")
                    if isinstance(sec, (int, float)) and self.live_steps and sec > 0:
                        last = self.live_steps[-1]
                        if last.get("timer_seconds") is None:
                            last["timer_seconds"] = int(sec)
                            self.active_stopwatch_label = None
                            # If we already finalized, update current_recipe and re-push so total_time_minutes is set
                            if self.current_recipe and self.current_recipe.get("description") == "Documented live by Mise while you cooked.":
                                steps = self.current_recipe.get("steps", [])
                                if steps and len(steps) == len(self.live_steps):
                                    steps[-1]["timer_seconds"] = int(sec)
                                    total_seconds = sum(s.get("timer_seconds") or 0 for s in self.live_steps)
                                    self.current_recipe["total_time_minutes"] = round(total_seconds / 60) if total_seconds else None
                                    try:
                                        await websocket.send_text(json.dumps({
                                            "type": "recipe",
                                            "recipe": self.current_recipe,
                                            "source": "documented",
                                        }))
                                    except Exception:
                                        pass
        except WebSocketDisconnect:
            print(f"[mise] browser disconnected (audio={audio_in_count}, video={video_in_count})", flush=True)
        except Exception as e:
            print(f"[mise] receive error: {type(e).__name__}: {str(e)[:120]}", flush=True)
            if "1011" in str(e):
                raise

    async def _send_to_browser(self, websocket, session):
        """Forward Gemini responses → browser."""
        audio_count = 0
        response_count = 0
        seen_tool_calls: set[tuple] = set()  # dedup within a turn
        sent_transcription_this_turn = False  # True once output_transcription fired; suppresses model_turn text
        while True:
          async for response in session.receive():
            response_count += 1
            try:
                sc = response.server_content
                interrupted = sc and getattr(sc, "interrupted", False)

                # Interrupted: tell client first and do NOT send this response's audio (so playback stops cleanly)
                if interrupted:
                    await websocket.send_text(json.dumps({"type": "interrupted"}))
                    print("[mise] interrupt detected → client should stop playback", flush=True)

                # Audio (skip if this response was an interrupt so client doesn't enqueue then immediately stop)
                if response.data and not interrupted:
                    audio_count += 1
                    if audio_count == 1:
                        print("[mise] first audio chunk received ✓", flush=True)
                    await websocket.send_text(json.dumps({
                        "type": "audio",
                        "data": base64.b64encode(response.data).decode("utf-8"),
                    }))

                # Audio transcription (preferred path for native audio models)
                if sc and sc.output_transcription and sc.output_transcription.text:
                    txt = _sanitize_transcript(sc.output_transcription.text)
                    if txt:
                        sent_transcription_this_turn = True
                        await websocket.send_text(json.dumps({
                            "type": "transcript", "text": txt,
                        }))

                # Fallback: text parts from model_turn — only if output_transcription never fired this turn
                # (avoids duplicate when the API sends the same text via both paths in separate response objects)
                elif sc and sc.model_turn and not sent_transcription_this_turn:
                    for part in (sc.model_turn.parts or []):
                        if part.text and not getattr(part, "thought", False):
                            txt = _sanitize_transcript(part.text)
                            if txt:
                                await websocket.send_text(json.dumps({
                                    "type": "transcript", "text": txt,
                                }))

                # Turn complete
                if sc and sc.turn_complete:
                    chunks_user_sent_this_turn = getattr(self, "_audio_chunks_since_last_turn", 0)
                    self._audio_chunks_since_last_turn = 0
                    self._turns_completed = getattr(self, "_turns_completed", 0) + 1
                    if audio_count == 0:
                        total_sent = getattr(self, "_audio_chunks_sent_to_session", 0)
                        print(
                            f"[mise] turn complete (0 audio — model sent no speech) | user had sent {chunks_user_sent_this_turn} chunks this turn, {total_sent} total to session",
                            flush=True,
                        )
                    else:
                        print(f"[mise] turn complete ({audio_count} audio, {response_count} total responses so far)", flush=True)
                    audio_count = 0
                    sent_transcription_this_turn = False
                    seen_tool_calls.clear()
                    await websocket.send_text(json.dumps({"type": "turn_complete"}))

                # Tool calls — collect all responses and send as a batch
                if response.tool_call:
                    # If the model already spoke this turn, seal that transcript entry
                    # before processing the tool — the model will speak again after the
                    # tool result, and we want it to appear as a separate message.
                    if sent_transcription_this_turn:
                        await websocket.send_text(json.dumps({"type": "turn_complete"}))
                        sent_transcription_this_turn = False
                    tool_responses = []
                    for fc in response.tool_call.function_calls:
                        args = _deep_convert(fc.args) if fc.args else {}
                        dedup_key = (fc.name, json.dumps(args, sort_keys=True))
                        if dedup_key in seen_tool_calls:
                            print(f"[mise] skipping duplicate tool call: {fc.name}", flush=True)
                            # Still need to ack it so the model isn't left hanging
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue
                        seen_tool_calls.add(dedup_key)
                        print(f"[mise] tool: {fc.name} args={json.dumps(args)[:120]}", flush=True)

                        if fc.name == "fetch_recipe":
                            dish = args.get("dish_name", "")
                            await websocket.send_text(json.dumps({"type": "recipe_search_start", "query": dish}))
                            try:
                                async with httpx.AsyncClient(timeout=30) as hx:
                                    resp = await hx.post(
                                        f"{RECIPE_AGENT_URL}/parse",
                                        json={"input": dish, "persona": self.persona},
                                    )
                                    resp.raise_for_status()
                                    data = resp.json()
                                self.current_recipe = data["recipe"]
                                await websocket.send_text(json.dumps({
                                    "type": "recipe",
                                    "recipe": data["recipe"],
                                    "source": data.get("source", "generated"),
                                }))
                                tool_responses.append(types.FunctionResponse(
                                    name=fc.name,
                                    response={"recipe": data["recipe"]},
                                    id=fc.id,
                                ))
                                continue
                            except Exception as e:
                                err_msg = str(e).strip() or repr(e)
                                body = ""
                                if hasattr(e, "response") and e.response is not None:
                                    try:
                                        r = e.response
                                        body = (getattr(r, "text", None) or getattr(r, "content", b"") or b"")[:500]
                                        if isinstance(body, bytes):
                                            body = body.decode("utf-8", errors="replace")
                                        if hasattr(r, "status_code") and r.status_code and not err_msg:
                                            err_msg = f"HTTP {r.status_code}"
                                    except Exception:
                                        pass
                                print(f"[mise] fetch_recipe failed: {err_msg}", flush=True)
                                if body:
                                    print(f"[mise] response body: {body}", flush=True)
                                await websocket.send_text(json.dumps({"type": "recipe_search_failed", "error": err_msg}))
                                tool_responses.append(types.FunctionResponse(
                                    name=fc.name,
                                    response={"error": err_msg},
                                    id=fc.id,
                                ))
                                continue

                        elif fc.name == "start_stopwatch":
                            if self.document_mode_started_at is None:
                                self.document_mode_started_at = time.time()
                            self.active_stopwatch_label = args.get("label", "Step")
                            # fall through to send tool_call to frontend

                        elif fc.name == "set_draft_name":
                            self.draft_name = (args.get("name") or "").strip() or None
                            if self.draft_name:
                                if self.document_mode_started_at is None:
                                    self.document_mode_started_at = time.time()
                                _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                _write_draft(self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc)
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "add_live_step":
                            if self.document_mode_started_at is None:
                                self.document_mode_started_at = time.time()
                            step_args = dict(args)
                            if step_args.get("timer_seconds") is not None and step_args["timer_seconds"] <= 0:
                                del step_args["timer_seconds"]
                            # Deduplicate: skip if instruction is identical to the last recorded step
                            _instr = (step_args.get("instruction") or "").strip().lower()
                            if self.live_steps and (self.live_steps[-1].get("instruction") or "").strip().lower() == _instr:
                                tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                                continue
                            self.live_steps.append(step_args)
                            _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                            asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc))
                            await websocket.send_text(json.dumps({
                                "type": "live_step",
                                "step": step_args,
                                "step_number": len(self.live_steps),
                            }))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "delete_live_step":
                            step_num = int(args.get("step_number", 0))
                            if 1 <= step_num <= len(self.live_steps):
                                self.live_steps.pop(step_num - 1)
                                _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc))
                                await websocket.send_text(json.dumps({
                                    "type": "delete_live_step",
                                    "step_number": step_num,
                                }))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "edit_live_step":
                            step_num = int(args.get("step_number", 0))
                            if 1 <= step_num <= len(self.live_steps):
                                updated = dict(self.live_steps[step_num - 1])
                                updated["instruction"] = (args.get("instruction") or "").strip()
                                if args.get("timer_seconds") is not None:
                                    updated["timer_seconds"] = args["timer_seconds"]
                                self.live_steps[step_num - 1] = updated
                                _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc))
                                await websocket.send_text(json.dumps({
                                    "type": "edit_live_step",
                                    "step_number": step_num,
                                    "step": updated,
                                }))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "add_live_ingredient":
                            ing = {
                                "amount": (args.get("amount") or "").strip(),
                                "item": (args.get("item") or "").strip(),
                                "prep": (args.get("prep") or "").strip(),
                            }
                            if ing["item"]:
                                self.live_ingredients.append(ing)
                                _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc))
                                await websocket.send_text(json.dumps({
                                    "type": "live_ingredient",
                                    "ingredient": ing,
                                }))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "finalize_live_recipe":
                            total_elapsed = self.draft_accumulated_seconds
                            if self.document_mode_started_at is not None:
                                total_elapsed += time.time() - self.document_mode_started_at
                            recipe = _build_live_recipe(
                                (args.get("name") or self.draft_name or "My Cook").strip() or "My Cook",
                                self.live_steps,
                                session_start_ts=None,
                                ingredients=self.live_ingredients,
                                total_elapsed_seconds=total_elapsed if total_elapsed > 0 else None,
                            )
                            self.current_recipe = recipe
                            await websocket.send_text(json.dumps({
                                "type": "recipe",
                                "recipe": recipe,
                                "source": "documented",
                            }))
                            name = recipe.get("name", "Recipe")
                            if self._saved_recipe_name_this_session != name:
                                _save_recipe_locally(recipe, photos=self.step_photos)
                                self._saved_recipe_name_this_session = name
                                await websocket.send_text(json.dumps({"type": "recipe_saved"}))
                            _clear_draft()
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "save_recipe_to_library":
                            # If no finalized recipe but we have live steps (e.g. user said "save it" before finalize), build and save now
                            if not self.current_recipe and self.live_steps:
                                _total = self.draft_accumulated_seconds
                                if self.document_mode_started_at is not None:
                                    _total += time.time() - self.document_mode_started_at
                                recipe = _build_live_recipe(
                                    "My Cook",
                                    self.live_steps,
                                    session_start_ts=None,
                                    ingredients=self.live_ingredients,
                                    total_elapsed_seconds=_total if _total > 0 else None,
                                )
                                self.current_recipe = recipe
                            if self.current_recipe:
                                name = self.current_recipe.get("name", "Recipe")
                                if self._saved_recipe_name_this_session != name:
                                    _save_recipe_locally(self.current_recipe, photos=self.step_photos)
                                    self._saved_recipe_name_this_session = name
                                await websocket.send_text(json.dumps({"type": "recipe_saved"}))
                                tool_responses.append(types.FunctionResponse(
                                    name=fc.name,
                                    response={"result": "saved", "name": name},
                                    id=fc.id,
                                ))
                            else:
                                tool_responses.append(types.FunctionResponse(
                                    name=fc.name,
                                    response={"result": "no_recipe", "message": "No recipe is loaded to save."},
                                    id=fc.id,
                                ))
                            continue

                        elif fc.name == "edit_ingredient":
                            idx = args.get("index")
                            if idx is not None:
                                ing = {
                                    "amount": (args.get("amount") or "").strip(),
                                    "item": (args.get("item") or "").strip(),
                                    "prep": (args.get("prep") or "").strip(),
                                }
                                await websocket.send_text(json.dumps({"type": "edit_ingredient", "index": idx, "ingredient": ing}))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "edit_step":
                            step_num = args.get("step_number")
                            if step_num is not None:
                                step = {
                                    "instruction": (args.get("instruction") or "").strip(),
                                    "timer_seconds": args.get("timer_seconds"),
                                }
                                await websocket.send_text(json.dumps({"type": "edit_step", "step_number": step_num, "step": step}))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "play_music":
                            query = (args.get("query") or "").strip()
                            if query:
                                video_id = await _youtube_search(query)
                                await websocket.send_text(json.dumps({"type": "play_music", "query": query, "videoId": video_id}))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "playing", "query": query}, id=fc.id))
                            continue

                        elif fc.name == "stop_music":
                            await websocket.send_text(json.dumps({"type": "stop_music"}))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "stopped"}, id=fc.id))
                            continue

                        elif fc.name == "set_music_volume":
                            vol = max(0, min(100, int(args.get("volume", 50))))
                            await websocket.send_text(json.dumps({"type": "set_music_volume", "volume": vol}))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok", "volume": vol}, id=fc.id))
                            continue

                        elif fc.name == "capture_step_photo":
                            step_num = args.get("step_number")  # optional: null for on-demand photos
                            if self.last_video_frame:
                                self.step_photos.append({"step_id": step_num, "data": self.last_video_frame})
                                print(f"[mise] photo captured (step={step_num}); total {len(self.step_photos)} photo(s)", flush=True)
                                # Persist photo to draft immediately so it survives if the session ends before the next step
                                if self.live_steps or self.live_ingredients:
                                    _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                    asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc))
                                # Notify user immediately; photo is saved even if Live API drops with 1008 after tool response
                                try:
                                    await websocket.send_text(json.dumps({"type": "photo_captured", "step_number": step_num}))
                                except Exception:
                                    pass
                                # Minimal response to reduce chance of Live API 1008 (known issue with function calls)
                                tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            else:
                                tool_responses.append(types.FunctionResponse(
                                    name=fc.name,
                                    response={"result": "skipped", "message": "No camera frame available yet."},
                                    id=fc.id,
                                ))
                            continue

                        await websocket.send_text(json.dumps({
                            "type": "tool_call",
                            "name": fc.name,
                            "args": args,
                            "call_id": fc.id,
                        }))
                        tool_responses.append(
                            types.FunctionResponse(
                                name=fc.name,
                                response={"result": "ok"},
                                id=fc.id,
                            )
                        )

                    # Send ALL tool responses at once so model can continue
                    print(f"[mise] sending {len(tool_responses)} tool response(s)…", flush=True)
                    await session.send_tool_response(
                        function_responses=tool_responses
                    )
                    print(f"[mise] tool responses sent ✓", flush=True)
            except Exception as e:
                if "websocket.close" not in str(e) and "websocket.send" not in str(e):
                    print(f"[mise] send error: {e}", flush=True)
                return

    async def close(self):
        pass
