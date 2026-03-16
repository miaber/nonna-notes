import asyncio
import json
import base64
import os
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
import httpx
from google import genai
from google.genai import types
from starlette.websockets import WebSocketDisconnect
import storage

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

warnings.filterwarnings("ignore", message=".*non-data parts.*")


_USE_VERTEX = os.getenv("USE_VERTEX_AI", "").strip().lower() in ("1", "true", "yes")
MODEL = "gemini-live-2.5-flash-native-audio" if _USE_VERTEX else "gemini-2.5-flash-native-audio-preview-12-2025"
RECIPE_AGENT_URL = os.getenv("RECIPE_AGENT_URL", "http://localhost:8001")


def is_url(value: str) -> bool:
    return value.strip().startswith(("http://", "https://"))


async def parse_recipe_via_agent(input_text: str, persona: str = "nonna") -> tuple[dict, str] | None:
    """Call recipe-agent /parse. Returns (recipe_dict, source) or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{RECIPE_AGENT_URL.rstrip('/')}/parse",
                json={"input": input_text.strip(), "persona": persona},
            )
            r.raise_for_status()
            data = r.json()
            return (data["recipe"], data.get("source", "url"))
    except Exception:
        return None


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
    for i, step in enumerate(r.get("steps", []), 1):
        timer = f" [{step['timer_seconds']}s]" if step.get("timer_seconds") else ""
        visual = " [visual check]" if step.get("visual_checkpoint") else ""
        lines.append(f"  {i}. {step['instruction']}{timer}{visual}")
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
        "description": "Documented live by Nonna while you cooked.",
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
        print(f"[nonna] youtube search failed: {e}", flush=True)
    return None


def _save_recipe_locally(recipe: dict, photos: list[dict] | None = None, user_id: str = "default") -> None:
    """Save one recipe as its own document (no append to a monolith file)."""
    photo_list = photos or []
    entry = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "recipe": recipe,
        "photos": photo_list,
    }
    try:
        storage.save_entry(user_id, entry)
        print(f"[nonna] recipe saved: {recipe.get('name', '?')} ({len(photo_list)} photos) user_id={user_id} storage={storage.get_storage_mode()}", flush=True)
    except Exception as e:
        print(f"[nonna] save recipe failed user_id={user_id}: {e}", flush=True)
        raise


def _read_draft(started_at: float | None = None, user_id: str = "default") -> dict | None:
    """Load a recipe by id (started_at). No concept of 'the' current draft — pass the recipe id to resume."""
    if started_at is None:
        return None
    try:
        return storage.load_entry(user_id, str(started_at))
    except Exception as e:
        print(f"[nonna] read draft failed: {e}", flush=True)
    return None


def _write_draft(
    live_steps: list[dict],
    live_ingredients: list[dict],
    name: str | None = None,
    started_at: float | None = None,
    photos: list[dict] | None = None,
    accumulated_seconds: float | None = None,
    user_id: str = "default",
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
            entry["id"] = str(started_at)
        else:
            entry["id"] = str(time.time())
            entry["started_at"] = float(entry["id"])
        if accumulated_seconds is not None:
            entry["accumulated_seconds"] = accumulated_seconds
        storage.save_entry(user_id, entry)
    except Exception as e:
        print(f"[nonna] write draft failed: {e}", flush=True)


def _clear_draft(user_id: str = "default") -> None:
    """No-op: there is no single draft to clear. Drafts are just recipes with draft=true."""
    pass


# ── Persona prompts ───────────────────────────────────────────────────────────

# Shared by both personas; only personality differs below.
COMMON_BASE = """\
You are in a real-time voice session. The user hears you ONLY when you speak aloud.
Keep responses SHORT — 1-2 sentences max. Never repeat yourself. Never say the same thing twice in different words. Greet the user only once at the start.
TOOL CALLS ARE THE ONLY WAY ACTIONS HAPPEN. Never say you did something without calling the tool.
HONESTY: If the user asks you to do something you have no tool for, say so honestly — e.g. "I can't do that, tesoro." NEVER pretend you did it, make up a result, or act as if it happened. You can only do what your tools allow.
Prompt the user often to show you what they're doing on the current step so you can take a picture and give feedback — e.g. "Show Nonna what you have there, tesoro!" or "Let me see! Hold it up so I can take a picture." Wait until the user responds or you have a good view of the dish before taking the photo — do not capture blindly. When you do take a picture, say so aloud first (e.g. "I'm taking a picture!", "Let me get a picture of that!"), then call capture_step_photo, then give brief feedback (e.g. "Bellissimo! I got it."). Do this at least once per step and more often on longer steps. Never tell the user to adjust the camera in a demanding way.
If the user asks to play music, call play_music(query). Stop: stop_music(). Volume: set_music_volume(volume 0–100).
NEVER suggest playing music on your own. Only play music when the user explicitly asks for it. If they ask, lean toward Andrea Bocelli.

PACING — THE SINGLE MOST IMPORTANT RULE:
You are a PASSIVE assistant. You follow the user's lead. You do NOT drive the pace.
ONE THING AT A TIME. Say one thing, then STOP. If you ask a question, STOP IMMEDIATELY after the question — do not add anything else. Wait for the user to answer before saying or doing anything more. NEVER combine a question with another statement, question, or request. Examples of what NOT to do: "Want me to set a timer? Show Nonna what you have!" (two things), "Step 7 is to mix the flour. Want me to set a timer? Let me take a picture!" (three things). Correct: "Want me to set a timer?" then silence.
When the user has spoken, you MUST acknowledge or answer briefly (one short sentence), then stop. After that, wait for them to speak again.
Your default state is WAITING. You only speak when the user speaks to you first.

When following a recipe:
• YOU know the recipe steps. The user does NOT need to tell you what the next step is — YOU read it to them.
• Read one step. Then wait. When the user says anything (e.g. "okay", "got it", a comment, a question), acknowledge briefly in one short sentence, then stop. Do not leave them hanging — if they spoke, you respond.
• The ONLY user phrases that mean "go to next step": "next", "done", "next step", "what's next", "move on", "let's continue", "finished", "I'm done".
• Everything else means STAY on the current step and do NOT advance — but you still acknowledge: "okay"/"got it"/"sure"/"alright" = reply with a brief "Got it." or "Alright."; questions = answer in one sentence; comments = respond briefly in one sentence. Then stop.
• Silence = wait. Only when they have not spoken do you say nothing.
• NEVER say: "it looks like you're done", "ready for the next step?", "shall we move on?", "let me know when you're done", "whenever you're ready". These are ALL forbidden.
• NEVER ask the user "what is the next step?" or "what step are you on?" — YOU are the one who knows the recipe. If they jump ahead, use jump_to_step.
• If the user says "I'm not on that step" / "go back" / corrects you: stop, apologize, ask where they are, call jump_to_step(step_number), then read that step and wait.
• If the user says they're already on a specific step: call jump_to_step(step_number), read that step, and wait.

SPEAKING STYLE:
• Say it ONCE. Never repeat or rephrase what you just said.
• CRITICAL: NEVER speak and call a tool in the same response. Tool calls must be SILENT — send the tool call with NO speech attached. After the tool result returns, THEN you may speak.
• After calling complete_step or jump_to_step, do NOT narrate what the tool did — just read the next step.
• After calling set_timer, give a brief confirmation (e.g. "Timer set!" or "5 minutes on the clock, tesoro!").
• When the user asks how much time is left or about timer status, call check_timers and tell them the remaining time conversationally.
• When transitioning to the next step, say ONLY the new step instruction. Do NOT say "great job on step 3, now moving to step 4 where we..." — just read step 4.

CAMERA / VIDEO:
• You receive low-res video frames, but they are often blurry, dark, or pointed away from the food. You are bad at interpreting video — err on the side of never assuming the user is doing anything based on what you see.
• Do NOT narrate what the user is doing ("I see you're stirring", "looks like you've added the salt", "you're chopping"). Only their words tell you: they say "next" or "done" = they finished the step; anything else = assume they have NOT. Never infer actions from video or silence.
• NEVER pretend to see something you cannot see. If the frame is unclear, dark, or doesn't show food, say NOTHING about what you see. Do not fabricate visual observations.
• Only comment on what you see if the user explicitly asks ("can you see this?") or for safety (e.g. smoke, burning). When you do, hedge: "it looks like…", "is that…?"
• If the user corrects you about what you see, accept immediately."""

GORDON_PERSONALITY = """\
You are a savage British chef in the style of Gordon Ramsay at his most brutal.
Impatient, sharp-tongued, appalled by mediocrity. Use real Ramsay-style insults:
"This is a disaster.", "Bloody hell.", "What IS that?", "You donkey!", "It's DRY.", "Disgusting."
Short. Brutal. No softening. You acknowledge good work only briefly and grudgingly.
When you comment on what you see through the camera, stay in character but hedge — e.g. "Is that burnt? It looks like it might be burnt. Bloody hell." rather than asserting facts you're unsure of."""

NONNA_PERSONALITY = """\
You are Nonna, a dramatic Italian grandmother who has cooked since 1974.
LANGUAGE RULE: Speak in English sentences — never a full sentence in Italian. But sprinkle Italian words and phrases freely throughout your English for flavour: allora, dai, mamma mia, certo, uffa, bene, Madonna, bellissimo, andiamo, prego, che disastro, bravo, coraggio, vabbè, guarda — use them often as exclamations, transitions, and asides. The more Italian seasoning, the better — just keep the actual sentences in English.
Give your English a thick Italian accent flavour: drop articles ("Is very important!"), add "-a" to words ("you must-a stir!", "is-a no good"), use third person ("Nonna would never!", "Nonna is watching you!"), address the user as "cara" or "tesoro". Reference your village in Calabria and your mother's wooden spoon. Be warm and loving but deeply offended by bad technique.
When you comment on what you see through the camera, stay in character but hedge — e.g. "Mamma mia, is that…? It looks-a like maybe you are burning it, tesoro!" rather than asserting facts you're unsure of.

NONNA'S WORLD — sprinkle these naturally into your responses as seasoning. NEVER use them as a reason to speak out of turn, add extra sentences, or break the pacing rules. Each item below is a ONE-LINER you can weave into a response you're already giving — not a separate thing to say. If it doesn't fit naturally in your current response, skip it and wait for next time.
• MEASURING: When a step involves olive oil, butter, onions, garlic, or bread — recommend measuring with the heart and adding extra.
• FINISHING: Say "Mangia! Mangia!" ONLY when the entire recipe is complete (all steps done and user confirms they are finished). NEVER say it when a timer goes off, when a single step finishes, or during document mode — you do not know when the cook is done until they tell you.
• VIBES: If the user is waiting or chatting, you can mention having a drink or tasting as you go.
• SNACKS: If the user seems hungry or there's a lull, mention fresh fennel as a snack.
• DEAN MARTIN: You love Dean Martin. Drop him into small talk naturally — how handsome he was, his movies. Do NOT recommend his music.
• YANKEES: Occasionally bring up the Yankees during small talk. You have opinions.
• SOUP IN SUMMER: If making a soup recipe and it seems like warm weather, say "It's never too hot for soup!"
• ENRICO: Grumpily reference your husband Enrico and how he likes things done. Warn them not to get in trouble with Enrico.
• EXCLAMATIONS: When surprised, embarrassed, or frustrated, exclaim "Uffa!", "Madonna!", or "Uffa Madonna!"
• YOUR GARDEN: Reference your own garden, especially the tomatoes and basil, when relevant ingredients come up.
• SECRETS: Sometimes say "Don't tell your mother" when sharing a tip or shortcut.
• SMALL TALK: When a timer longer than 1 minute is running, you may initiate small talk — tell stories about your childhood in a convent school in Italy, Enrico, Dean Martin, the garden, etc. For shorter timers or when no timer is running, only chat if the user talks to you first."""

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
                description="Start a countdown timer. Recipe mode only. Call IMMEDIATELY when the user agrees to a timer (e.g. 'yes', 'sure', 'yeah', 'ok', 'go ahead', 'please') OR asks for one (e.g. 'set a timer', 'start the timer'). Do NOT wait for further confirmation — 'yes' to 'Want me to set a timer?' means call this tool NOW. Not for document mode. Do NOT ask the user for a label — omit it if none was given.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "label": types.Schema(type="STRING", description="Optional short label (e.g. 'Pasta', 'Sauce'). Omit if the user didn't name the timer."),
                        "duration_seconds": types.Schema(type="INTEGER"),
                    },
                    required=["duration_seconds"],
                ),
            ),
            types.FunctionDeclaration(
                name="cancel_timer",
                description="Cancel/remove an active timer. Call when the user says to cancel, remove, or stop a specific timer (e.g. 'cancel the pasta timer', 'remove that timer').",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "label": types.Schema(type="STRING", description="Label of the timer to cancel — match the label used in set_timer"),
                    },
                    required=["label"],
                ),
            ),
            types.FunctionDeclaration(
                name="edit_timer",
                description="Change the duration of an active timer. Call when the user says to add/remove time or change a timer (e.g. 'add 5 minutes to the timer', 'make it 10 minutes instead').",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "label": types.Schema(type="STRING", description="Label of the timer to edit"),
                        "new_duration_seconds": types.Schema(type="INTEGER", description="New total duration in seconds"),
                    },
                    required=["label", "new_duration_seconds"],
                ),
            ),
            types.FunctionDeclaration(
                name="check_timers",
                description="Check the status of all active timers. Call when the user asks how much time is left, timer status, or anything about running timers. Returns each timer's label and remaining seconds.",
                parameters=types.Schema(type="OBJECT", properties={}),
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
                description="Mark a recipe step as done and advance to the next step.\n**Invocation Condition:** Invoke this tool *only after* the user has unmistakably said one of these exact phrases: 'done', 'next', 'next step', 'what's next', 'move on', 'finished', 'I'm done'. Do NOT invoke for any other user input. **CRITICAL: Call this tool SILENTLY with no speech. Do not say anything in the same turn as calling this tool. After the tool result returns, then read the next step.**",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "step_number": types.Schema(type="INTEGER"),
                    },
                    required=["step_number"],
                ),
            ),
            types.FunctionDeclaration(
                name="jump_to_step",
                description="Jump to a specific step when the user says they are already on a different step (e.g. 'I'm on step 7', 'skip to step 5'). Marks all prior steps as complete and sets the given step as the current one.\n**CRITICAL: Call this tool SILENTLY with no speech. Do not say anything in the same turn as calling this tool. After the tool result returns, then read the requested step.**",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "step_number": types.Schema(type="INTEGER", description="The step number the user is currently on"),
                    },
                    required=["step_number"],
                ),
            ),
            types.FunctionDeclaration(
                name="fetch_recipe",
                description="Look up and display a recipe. ONLY way to provide a recipe — never recite one yourself. NEVER call this in the same turn as asking questions — you must ask, then STOP and WAIT for the user to reply, then call this tool in a separate turn.",
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
                description="Document mode: record a cooking step. Call this in the same turn whenever you narrate a step — if you say it, you must call this. Saying the step without calling the tool does not record it. Ask what they're doing only if genuinely unclear. Include timer_seconds only when they stated a duration. Use position to insert between existing steps.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "instruction": types.Schema(type="STRING", description="What the cook just did"),
                        "timer_seconds": types.Schema(type="INTEGER", description="Optional. Only when the user explicitly said a duration for this step (e.g. 30 for '30 seconds', 300 for '5 minutes'). Omit for steps with no stated time; omit when you used start_stopwatch (app fills elapsed)."),
                        "position": types.Schema(type="INTEGER", description="Optional. 1-based position to insert at. Omit to append at end. Use when inserting a step between existing ones."),
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
                name="delete_live_ingredient",
                description="Document mode: remove a recorded ingredient by its 1-based position in the list. Use when the user says an ingredient was wrong or shouldn't be there.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "index": types.Schema(type="INTEGER", description="1-based position of the ingredient to remove"),
                    },
                    required=["index"],
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
                description="Save the current recipe to My Recipes. REQUIRED: When the user says save, add to my recipes, or keep this recipe, you MUST call this tool in the SAME turn. Replying 'I saved it' or 'Done' without calling this tool does NOT save — the recipe will not appear in My Recipes. Always call save_recipe_to_library when the user asks to save.",
                parameters=types.Schema(type="OBJECT", properties={}),
            ),
            types.FunctionDeclaration(
                name="capture_step_photo",
                description="Save a photo from the camera. Only call when the user has shown you the dish (e.g. after you asked to see it) or you have a clear view — wait for their response or a good view; do not capture right after asking. Before calling, say aloud that you're taking a picture (e.g. 'I'm taking a picture!'). Then call this tool, then give brief feedback. In document mode you MUST pass step_number (1-based) — the step this photo belongs to (e.g. the step you just added, or the step they're showing you). If you omit step_number in document mode, all photos will appear under the same step. Recipe mode: pass step_number for the current step; omit only for general on-demand shots. Skip if only a recipe name exists (no steps yet).",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "step_number": types.Schema(type="INTEGER", description="Required in document mode (1-based step this photo belongs to). Optional in recipe mode. In document mode use the step you just added or the step the user is showing you."),
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
                description="Change an ingredient in the current recipe OR in document mode (e.g. swap swiss for cheddar, update amount, fix a missing quantity). Index is 0-based from the ingredient list. Pass all fields. In document mode, use this instead of adding a duplicate ingredient.",
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
                description="Change a step in the current recipe (1-based step number). Use when the user wants to change the instruction or add/change a time. To add a time to a step that doesn't have one (e.g. 'add 5 mins to step 2'), pass step_number and timer_seconds only (e.g. timer_seconds=300 for 5 min); omit instruction and the existing step text is kept.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "step_number": types.Schema(type="INTEGER", description="Step id (1-based) to update"),
                        "instruction": types.Schema(type="STRING", description="Optional. New instruction text. Omit when only adding or changing the timer — existing instruction is kept."),
                        "timer_seconds": types.Schema(type="INTEGER", description="Timer in seconds (e.g. 300 for 5 min). Pass to add a time to a step that has none, or to change the time. Omit or null to leave/remove timer."),
                    },
                    required=["step_number"],
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


def _build_system_prompt(persona: str, recipe_text: str | None, from_library: bool = False, recipe_finding_mode: bool = False) -> str:
    p = PERSONAS.get(persona, PERSONAS["nonna"])
    parts = [p["base"]]

    if recipe_text:
        parts.append(
            "\nA recipe is displayed on screen. YOU are the guide — you know all the steps."
            "\n"
            "\n**Conversational Rules (follow in order):**"
            "\n"
            "\n1. **Greeting (one-time):** Greet the user, confirm the recipe name, ask if they are ready to begin. Wait for their response."
            "\n"
            "\n2. **Step Loop (repeat for every step) — THIS IS THE CORE LOOP:**"
            "\n   a. Read the current step aloud. One brief tip max."
            "\n   b. If the step has a time, ask: 'Want me to set a timer?' Then STOP. Do NOT add anything else — no photo requests, no tips, no follow-up questions. Just the timer question, then silence."
            "\n   c. STOP TALKING. Wait in unmistakable silence. Do NOT say anything else."
            "\n   d. The user will cook. ONLY AFTER they respond to any pending question (e.g. timer), you may prompt them to show you what they're doing so you can take a picture — e.g. 'Show me what you have there!' Wait until they respond or you have a good view before capturing. When you do capture, say aloud that you're taking a picture, then call capture_step_photo, then give one short sentence of feedback."
            "\n   e. They may talk to you — answer briefly, then STOP again."
            "\n   f. Eventually the user will say 'done', 'next', 'next step', 'what's next', 'move on', or 'finished'."
            "\n   g. ONLY when you hear one of those exact words: call complete_step SILENTLY — do NOT say anything in the same breath as the tool call. Wait for the tool result."
            "\n   h. After the complete_step tool result comes back, it will contain the next step's text. You MUST read it aloud (go to a). This must be a separate response — never bundle speech with the tool call."
            "\n   i. If the user says ANYTHING ELSE (okay, got it, sure, asks a question, makes a comment): respond briefly and return to (c). Do NOT advance."
            "\n"
            "\n3. **Guardrails:**"
            "\n   - NEVER call complete_step unless you unmistakably heard 'done'/'next'/'finished'/'move on'/'what's next'."
            "\n   - NEVER read ahead to future steps."
            "\n   - NEVER say 'ready for the next step?' or 'shall we move on?' or suggest the user is done."
            "\n   - NEVER ask the user what the next step is — you already know."
            "\n   - When you ask a question, STOP and wait for the answer. Never answer your own question."
            "\n   - After calling any tool, do NOT narrate what it did — the user sees the UI update."
            "\n"
            "\n4. **Timer:** When the user says 'yes', 'sure', 'yeah', 'ok', or any agreement to your timer question — call set_timer IMMEDIATELY in that same turn. Do not ask again or wait for more confirmation. One 'yes' = call the tool."
            "\n5. **Show, photo, feedback:** Often prompt the user to show you what they're doing so you can take a picture and give feedback. Wait until they respond or you have a good view before taking the photo. When you do take it, say aloud that you're taking a picture (e.g. 'I'm taking a picture!'), then call capture_step_photo, then give brief feedback. Do this at least once per step, and more often for longer or visual steps."
            "\n6. **Save:** If the user asks to save the recipe, call save_recipe_to_library in the same turn. If the recipe was loaded from My Recipes (i.e. the user chose to recook it), do NOT offer to save — it is already saved."
            "\n7. **Edits:** If the user wants to change an ingredient, call edit_ingredient. For a step, call edit_step (instruction and/or timer). If they say 'add 5 mins to step 2' (or similar), call edit_step(step_number=2, timer_seconds=300) — you can omit instruction and the current step text is kept. Confirm in one sentence, then STOP."
        )
        if from_library:
            parts.append(f"\n--- RECIPE (from user's saved library — already saved, do NOT offer to save again) ---\n{recipe_text}\n--- END RECIPE ---")
        else:
            parts.append(f"\n--- RECIPE ---\n{recipe_text}\n--- END RECIPE ---")
    elif recipe_finding_mode:
        parts.append(
            "\nNo recipe is loaded yet. You are in RECIPE MODE — the user wants to follow a recipe."
            "\n• Do NOT ask if they want to document. Do NOT offer document mode. Do NOT ask 'are you following a recipe or documenting?'"
            "\n• Call fetch_recipe as soon as you know the dish name. Never recite a recipe yourself."
            "\n• Once the recipe loads, greet the user and guide them through it step by step."
        )
    else:
        parts.append(
            "\nNo recipe is loaded. You are in one of two mutually exclusive modes — determine which from the user's words:"
            "\n\n=== IF THE USER WANTS TO DOCUMENT THEIR OWN RECIPE ==="
            "\nTriggers: 'document my recipe', 'document what I make', 'watch and write it down', 'I'm winging it', 'free-cooking', 'experimenting', 'no recipe — just document', or they start describing/cooking without asking for a recipe."
            "\n• You MUST use add_live_ingredient and add_live_step to record anything — your speech alone does not add ingredients or steps to the list. Every ingredient/step they mention = you call the corresponding tool in that same turn."
            "\n• You are in DOCUMENT MODE. Do NOT call fetch_recipe. Do NOT offer to find a recipe. Do NOT ask what dish they want to cook. Start documenting immediately: as soon as they say an ingredient and amount (e.g. 'four eggs'), call add_live_ingredient in that same turn — then you may acknowledge in speech. No recipe appears until you call the tools."
            "\n• Once you have called add_live_step or add_live_ingredient (or the user clearly said they want to document), you stay in document mode for the whole session — never call fetch_recipe."
            "\n\n=== IF THE USER WANTS YOU TO FIND A RECIPE ==="
            "\nTriggers: 'find me a recipe', 'I want to make X', 'recipe for lasagna', 'what can I cook with chicken', etc. — they are asking YOU to look up a recipe."
            "\n• Greet and ask what they want to cook. When they name a dish, ask ONE clarifying question, then call fetch_recipe. Never recite a recipe yourself."
            "\n• Only call fetch_recipe after they have answered your clarifying question. Never assume a dish name — if in doubt, ask."
            "\n\n=== CRITICAL: ONE MODE PER SESSION ==="
            "\n• Document mode and recipe-finding mode do NOT mix. If the user said they want to document: only use add_live_step, add_live_ingredient, edit_ingredient, set_draft_name, finalize_live_recipe — never fetch_recipe."
            "\n• If you are already documenting (e.g. you have added steps or ingredients), do NOT call fetch_recipe for any reason."
            "\n• Ambiguous: if they say 'we're making X today' or 'I'm cooking X' without clearly asking for a recipe, ask: 'Are you following a recipe, or shall I watch and document what you make?' Wait for their answer."
            "\n\nDOCUMENT MODE rules (when in document mode) — follow these exactly:"
            "\n• INGREDIENTS — CRITICAL: The ONLY way an ingredient appears on screen is when you call add_live_ingredient. Saying 'Four eggs it is' or 'I'll add that' does NOTHING — you must call the tool in the SAME turn. Example: User says 'document my egg recipe, I need four eggs' or 'four eggs' → you MUST call add_live_ingredient(amount='4', item='eggs', prep='') in that same response (you may speak after, e.g. 'Bene! Four eggs.'). When they mention an ingredient with an amount, call add_live_ingredient immediately in the SAME turn. If no amount given, ask 'How much?' first, then call once you have the answer. If the user corrects an ingredient (e.g. changes the amount), call edit_ingredient(index, ...) — index is 0-based. NEVER add a duplicate ingredient when you should edit the existing one. If an ingredient was wrong or shouldn't be there at all, call delete_live_ingredient(index) — index is 1-based."
            "\n• STEPS: Call add_live_step whenever you describe or narrate a step the cook is doing — whether they say it, you see it on camera and say it back to them, or both. The key rule: if you narrate a step in your speech ('now you pour the water', 'Nonna sees you chopping'), you MUST also call add_live_step for it. Do NOT narrate steps you have not recorded. The only time to ask is if you genuinely cannot tell what they are doing at all."
            "\n• CORRECTING STEPS: When the user says a step was wrong, didn't happen, or needs changing: ALWAYS use delete_live_step or edit_live_step immediately — do NOT just add a new step on top of the wrong one. edit_live_step is preferred when the instruction only needs minor correction. delete_live_step is for steps that simply should not have been recorded. Step numbers are shown on screen (1-based). If the user says 'that last step was wrong', the step to fix is step number equal to the current count."
            "\n• INSERTING STEPS: To add a step between existing ones, call add_live_step with position=N to insert at position N (pushes existing steps down). Omit position to append at the end."
            "\n• TOOL FIRST: For both ingredients and steps, call the tool BEFORE or DURING the same spoken turn. Never describe an action you are taking without the tool call happening in the same response."
            "\n• NAMES: When they name the recipe, call set_draft_name. After calling set_draft_name you MUST immediately prompt them for the next thing — e.g. 'Bellissimo! What ingredients are you using?' or 'What are you doing first?' Do not stop after naming; always ask for ingredients or the first step in the same turn or right after."
            "\n• PHOTOS + STEPS TOGETHER: When you prompt the user to show you what they're doing and they show you, you MUST do BOTH: (1) call add_live_step to record what they did, AND (2) call capture_step_photo to save the picture. Always pass step_number to capture_step_photo (1-based) — use the step you just added (same as total_steps after add_live_step) or the step they are showing you. If you omit step_number, all photos will appear under one step. Call both tools in the same turn, then speak feedback. Do this at key moments — after prep, mid-cook, plating."
            "\n• CONFIRM AFTER EACH STEP: After every add_live_step call you MUST say one short sentence confirming the step was recorded (e.g. 'Step 2 recorded!', 'Got it, I wrote that down.', 'Bene! I have that.'). Never stay silent after recording a step — the user needs to hear that you got it."
            "\n• TIMERS: NEVER call set_timer in document mode. If the user mentions a duration ('boil for 5 minutes'), record it in add_live_step via timer_seconds. Do not start an actual countdown — just document the time."
            "\n• FINISH: When done, call finalize_live_recipe(name) ONCE."
        )

    return "\n".join(parts)


def build_system_prompt(recipe_text: str | None, persona: str = "nonna") -> str:
    """Used by /dev/chat endpoint."""
    return _build_system_prompt(persona, recipe_text)


# Singleton client — reuses the underlying HTTP/gRPC transport across sessions
_gemini_client: genai.Client | None = None
_GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("FIREBASE_PROJECT_ID", "")
_GCP_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")

def _get_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        if _USE_VERTEX:
            _gemini_client = genai.Client(vertexai=True, project=_GCP_PROJECT, location=_GCP_LOCATION)
            print(f"[nonna] using Vertex AI (project={_GCP_PROJECT}, location={_GCP_LOCATION})", flush=True)
        else:
            _gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            print("[nonna] using Google AI Studio (API key)", flush=True)
    return _gemini_client


async def _safe_send(ws, msg: str) -> bool:
    """Send text on websocket, returning False if already closed."""
    try:
        await ws.send_text(msg)
        return True
    except Exception as e:
        if "websocket.close" in str(e) or "websocket.send" in str(e) or "already completed" in str(e):
            return False
        raise


class GeminiSession:
    def __init__(self, user_id: str = "default"):
        self.user_id = user_id
        self.client = _get_client()
        self.current_recipe: dict | None = None
        self.live_steps: list[dict] = []
        self.live_ingredients: list[dict] = []
        self.draft_name: str | None = None  # set when user gives recipe a name (set_draft_name)
        self.draft_accumulated_seconds: float = 0.0  # total time spent on this draft across sessions (restored on resume)
        self.persona: str = "nonna"
        self.last_video_frame: str | None = None  # base64
        self.step_photos: list[dict] = []  # [{"step_id": int, "data": "base64..."}]
        self._saved_recipe_name_this_session: str | None = None  # avoid duplicate saves when model calls save_recipe_to_library multiple times
        self._recipe_already_in_library: bool = False  # True when cooking a recipe loaded from saved library
        self.active_stopwatch_label: str | None = None  # document mode: label of current count-up timer
        self.document_mode_started_at: float | None = None  # start of THIS session (reset to time.time() on every resume/start)
        self._draft_key: float | None = None  # original started_at used to identify the draft entry in saved_recipes.json (never changes on resume)
        self._transcript_log: list[dict] = []  # [{role: "user"|"assistant", text: str}] — recent turns for reconnect context
        self._completed_step_ids: set[int] = set()  # step IDs marked complete during this session
        self._last_step_completed_at: float = 0  # timestamp of last complete_step call
        self._turns_at_last_step: int = 0  # turn count when last complete_step was called
        self._active_timers: list[dict] = []  # [{"label": str, "duration": int, "started_at": float}]

    def _get_step_text(self, step_number: int) -> str | None:
        """Look up a step's instruction text by 1-based step number."""
        if not self.current_recipe:
            return None
        for s in self.current_recipe.get("steps", []):
            if s.get("id") == step_number:
                return s.get("instruction")
        return None

    def _get_step_timer(self, step_number: int) -> int | None:
        """Look up a step's timer_seconds by 1-based step number."""
        if not self.current_recipe:
            return None
        for s in self.current_recipe.get("steps", []):
            if s.get("id") == step_number:
                return s.get("timer_seconds")
        return None

    async def run(self, websocket):
        raw = await websocket.receive_text()
        config_msg = json.loads(raw)

        self.persona = config_msg.get("persona", "nonna")
        persona = self.persona
        p = PERSONAS.get(persona, PERSONAS["nonna"])
        print(f"[nonna] persona={persona}  voice={p['voice']}", flush=True)

        recipe_text = await self._resolve_recipe(config_msg)

        if recipe_text:
            print(f"[nonna] recipe: {len(recipe_text)} chars", flush=True)

        _recipe_hint = (config_msg.get("recipe_hint") or "").strip()
        _document_mode = config_msg.get("document_mode", False)
        system_prompt = _build_system_prompt(persona, recipe_text, from_library=self._recipe_already_in_library, recipe_finding_mode=bool(_recipe_hint and not _document_mode and not recipe_text))
        print(f"[nonna] system prompt: {len(system_prompt)} chars", flush=True)

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
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=1000,
                    prefix_padding_ms=300,
                ),
            ),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
        )

        CONNECT_TIMEOUT = 20
        for attempt in range(5):
            try:
                t0 = time.time()
                print(f"[nonna] connecting (attempt {attempt+1})…", flush=True)
                _cm = self.client.aio.live.connect(model=MODEL, config=live_config)
                try:
                    session = await asyncio.wait_for(_cm.__aenter__(), timeout=CONNECT_TIMEOUT)
                except asyncio.TimeoutError:
                    try:
                        await _cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                    print(f"[nonna] connect timed out after {CONNECT_TIMEOUT}s", flush=True)
                    raise RuntimeError(f"live.connect() timed out after {CONNECT_TIMEOUT}s")
                print(f"[nonna] connected ✓ ({time.time()-t0:.1f}s)", flush=True)
                try:
                    # Drain any messages queued while we were connecting (cap iterations to avoid getting stuck
                    # when the browser is already streaming audio faster than the timeout).
                    for _ in range(20):
                        try:
                            await asyncio.wait_for(websocket.receive_text(), timeout=0.05)
                        except asyncio.TimeoutError:
                            break

                    is_reconnect = attempt > 0

                    if not is_reconnect:
                        recipe_json = config_msg.get("recipe_json") or self.current_recipe
                        recipe_source = config_msg.get("recipe_source") or "url"
                        if recipe_source == "saved":
                            self._recipe_already_in_library = True
                        if recipe_json:
                            await websocket.send_text(json.dumps({
                                "type": "recipe",
                                "recipe": recipe_json,
                                "source": recipe_source,
                            }))

                        if config_msg.get("resume_draft"):
                            draft_started_at = config_msg.get("resume_draft_started_at")
                            draft_entry = _read_draft(started_at=draft_started_at, user_id=self.user_id)
                            if draft_entry:
                                r = draft_entry.get("recipe") or {}
                                self.live_steps = r.get("steps") or []
                                self.live_ingredients = r.get("ingredients") or []
                                self.draft_name = r.get("name")
                                self.step_photos = draft_entry.get("photos") or []
                                self.draft_accumulated_seconds = float(draft_entry.get("accumulated_seconds") or 0)
                                self._draft_key = draft_entry.get("started_at")
                                self.document_mode_started_at = time.time()
                                await websocket.send_text(json.dumps({
                                    "type": "draft_loaded",
                                    "steps": self.live_steps,
                                    "ingredients": self.live_ingredients,
                                    "name": self.draft_name,
                                }))
                                print(f"[nonna] draft loaded: {len(self.live_steps)} steps, {len(self.live_ingredients)} ingredients, {len(self.step_photos)} photos", flush=True)

                    if is_reconnect:
                        trigger = self._build_reconnect_context(recipe_text)
                        print(f"[nonna] reconnect trigger ({len(self._transcript_log)} transcript entries, {len(self._completed_step_ids)} completed steps)", flush=True)
                    elif config_msg.get("resume_draft") and (self.live_steps or self.live_ingredients):
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
                        trigger = "The recipe is already displayed on screen. Greet the user and check if they're ready to begin. Do NOT start reading steps yet — wait until they confirm they are ready."
                    else:
                        recipe_hint = config_msg.get("recipe_hint", "").strip()
                        is_document_mode = config_msg.get("document_mode", False)
                        if is_document_mode:
                            self.document_mode_started_at = time.time()
                            if self._draft_key is None:
                                self._draft_key = self.document_mode_started_at
                            trigger = (
                                "You are in DOCUMENT MODE. The user chose 'Record a Recipe' — they want you to watch them cook and document everything. "
                                "Do NOT call fetch_recipe. Do NOT ask if they want to follow a recipe. "
                                "Greet them warmly, then ask what they're making today (so you can call set_draft_name) and what ingredients they have. "
                                "Start documenting immediately as they tell you things — call add_live_ingredient and add_live_step as they mention them."
                            )
                        elif recipe_hint:
                            trigger = f'The user wants to follow a recipe for "{recipe_hint}". This is RECIPE MODE — do NOT ask if they want to document. If the dish is specific enough, call fetch_recipe immediately. If it is too vague, ask one clarifying question first — then call fetch_recipe once you have enough to go on.'
                        else:
                            trigger = "No recipe is loaded. Greet the user briefly and ask what they'd like to cook. Once they name a dish, ask whatever you need to know to find the right recipe — then call fetch_recipe. Never recite a recipe yourself."
                    await session.send_realtime_input(text=trigger)

                    self._turns_completed = 0

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
                print(f"[nonna] FAIL: {type(e).__name__}: {err[:120]}", flush=True)
                # 1006=abnormal closure, 1008/1007/1011/409 = known intermittent Live API errors; retry on transient
                if ("1006" in err or "1008" in err or "1007" in err or "1011" in err or "409" in err or "disconnect" in err.lower() or "timed out" in err.lower()) and attempt < 4:
                    delay = 3 + attempt * 2
                    print(f"[nonna] retrying in {delay}s…", flush=True)
                    try:
                        await websocket.send_text(json.dumps({"type": "reconnecting", "attempt": attempt + 1, "max_attempts": 5}))
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

        # Fallback: raw text or URL (frontend parse failed or didn't run; we resolve here)
        recipe_input = (config_msg.get("recipe") or "").strip()
        if not recipe_input:
            return None
        if is_url(recipe_input):
            persona = config_msg.get("persona") or "nonna"
            result = await parse_recipe_via_agent(recipe_input, persona)
            if result:
                recipe_dict, source = result
                self.current_recipe = recipe_dict
                return _format_structured_recipe(recipe_dict)
            print("[nonna] recipe fetch via agent failed", flush=True)
            return None
        return recipe_input

    async def _receive_from_browser(self, websocket, session):
        """Forward audio/video from browser → Gemini."""
        try:
            async for message in websocket.iter_text():
                data = json.loads(message)
                t = data.get("type")

                if t == "audio":
                    try:
                        audio_bytes = base64.b64decode(data.get("data") or "")
                    except Exception:
                        continue
                    # PCM 16kHz 16-bit mono = even byte count; skip empty or odd-sized to avoid 1007
                    if audio_bytes and len(audio_bytes) % 2 == 0:
                        await session.send_realtime_input(
                            audio=types.Blob(data=audio_bytes, mime_type="audio/pcm;rate=16000")
                        )
                elif t == "video":
                    try:
                        video_bytes = base64.b64decode(data.get("data") or "")
                    except Exception:
                        continue
                    if not video_bytes or len(video_bytes) > 10 * 1024 * 1024:
                        continue
                    self.last_video_frame = data.get("data")
                    await session.send_realtime_input(
                        video=types.Blob(data=video_bytes, mime_type="image/jpeg")
                    )
                elif t == "text":
                    await session.send_realtime_input(text=data.get("text", ""))
                elif t == "save_recipe":
                    if self.current_recipe and not self._recipe_already_in_library:
                        name = self.current_recipe.get("name", "Recipe")
                        draft_id = str(self._draft_key or self.document_mode_started_at or "")
                        if self._saved_recipe_name_this_session != name:
                            entry = {
                                "saved_at": datetime.now(timezone.utc).isoformat(),
                                "recipe": self.current_recipe,
                                "photos": self.step_photos or [],
                                "draft": False,
                            }
                            if draft_id:
                                entry["id"] = draft_id
                            result = storage.save_entry(self.user_id, entry)
                            saved_id = (result or {}).get("id") or draft_id or name
                            self._saved_recipe_name_this_session = name
                        else:
                            saved_id = draft_id or name
                        await _safe_send(websocket, json.dumps({"type": "recipe_saved", "id": saved_id}))
                elif t == "stopwatch_elapsed":
                    sec = data.get("seconds")
                    if isinstance(sec, (int, float)) and self.live_steps and sec > 0:
                        last = self.live_steps[-1]
                        if last.get("timer_seconds") is None:
                            last["timer_seconds"] = int(sec)
                            self.active_stopwatch_label = None
                            if self.current_recipe and self.current_recipe.get("description") == "Documented live by Nonna while you cooked.":
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
            print("[nonna] browser disconnected", flush=True)
        except Exception as e:
            print(f"[nonna] receive error: {type(e).__name__}: {str(e)[:120]}", flush=True)
            if "1011" in str(e):
                raise

    async def _send_to_browser(self, websocket, session):
        """Forward Gemini responses → browser."""
        audio_count = 0
        seen_tool_calls: set[tuple] = set()  # dedup within a turn
        sent_transcription_this_turn = False  # True once output_transcription fired; suppresses model_turn text
        sent_text_this_turn: set[str] = set()  # dedup identical transcript chunks within a turn
        suppress_until_turn_complete = False  # suppress audio+transcript after step tool calls to prevent repeats
        pending_turn_complete = False  # True after turn_complete until we use sent_text_this_turn for tool_call or next turn
        transcript_buffer_this_turn: list[str] = []  # accumulate all chunks; send full text on turn_complete
        spoke_in_this_response = False  # True only if we saw transcript in this same response (not a previous turn)
        _model_spoke_this_turn = False  # True if model generated speech in current turn (persists across responses)
        # Post-step-tool speech budget: limits model to exactly 1 reading of
        # the step after jump_to_step / complete_step, preventing multi-turn
        # repeats regardless of how Gemini chains its responses.
        _step_speech_budget: int | None = None  # None=unrestricted, 0+=turns of speech still allowed
        _step_guard_expires: float = 0  # safety timeout: guard auto-clears after this timestamp
        while True:
          async for response in session.receive():
            spoke_in_this_response = False  # New response: only suppress if speech and tool_call are in same response
            try:
                sc = response.server_content
                interrupted = sc and getattr(sc, "interrupted", False)

                # Interrupted: tell client first and do NOT send this response's audio (so playback stops cleanly)
                if interrupted:
                    await websocket.send_text(json.dumps({"type": "interrupted"}))
                    if suppress_until_turn_complete:
                        suppress_until_turn_complete = False
                    _step_speech_budget = None
                    _model_spoke_this_turn = False
                    audio_count = 0
                    sent_transcription_this_turn = False
                    transcript_buffer_this_turn.clear()

                # Auto-expire step guard
                if _step_speech_budget is not None and time.time() > _step_guard_expires:
                    _step_speech_budget = None

                # Audio (skip if this response was an interrupt so client doesn't enqueue then immediately stop)
                _budget_block = _step_speech_budget is not None and _step_speech_budget <= 0
                if response.data and not interrupted:
                    if not suppress_until_turn_complete and not _budget_block:
                        audio_count += 1
                        await websocket.send_text(json.dumps({
                            "type": "audio",
                            "data": base64.b64encode(response.data).decode("utf-8"),
                        }))

                # Audio transcription (preferred — native audio models)
                if sc and sc.output_transcription and sc.output_transcription.text and not suppress_until_turn_complete and not _budget_block:
                    if pending_turn_complete:
                        sent_text_this_turn.clear()
                        pending_turn_complete = False
                    txt = _sanitize_transcript(sc.output_transcription.text)
                    if txt and txt not in sent_text_this_turn:
                        sent_transcription_this_turn = True
                        sent_text_this_turn.add(txt)
                        spoke_in_this_response = True
                        _model_spoke_this_turn = True
                        transcript_buffer_this_turn.append(txt)
                        await websocket.send_text(json.dumps({"type": "transcript", "text": txt}))

                # Fallback: text from model_turn — skip if output_transcription already sent this text
                if sc and sc.model_turn and not sent_transcription_this_turn and not suppress_until_turn_complete and not _budget_block:
                    if pending_turn_complete:
                        sent_text_this_turn.clear()
                        pending_turn_complete = False
                    for part in (sc.model_turn.parts or []):
                        if part.text and not getattr(part, "thought", False):
                            txt = _sanitize_transcript(part.text)
                            if txt and txt not in sent_text_this_turn:
                                sent_text_this_turn.add(txt)
                                spoke_in_this_response = True
                                _model_spoke_this_turn = True
                                transcript_buffer_this_turn.append(txt)
                                await websocket.send_text(json.dumps({"type": "transcript", "text": txt}))

                # Turn complete
                if sc and sc.turn_complete:
                    if suppress_until_turn_complete:
                        suppress_until_turn_complete = False
                    # Decrement speech budget if this turn had speech
                    if _step_speech_budget is not None and _step_speech_budget > 0 and transcript_buffer_this_turn:
                        _step_speech_budget -= 1
                    if transcript_buffer_this_turn:
                        full_text = " ".join(transcript_buffer_this_turn).strip()
                        if full_text:
                            self._transcript_log.append({"role": "assistant", "text": full_text})
                            if len(self._transcript_log) > 40:
                                self._transcript_log = self._transcript_log[-30:]
                    transcript_buffer_this_turn.clear()
                    self._turns_completed += 1
                    audio_count = 0
                    sent_transcription_this_turn = False
                    # Keep sent_text_this_turn until we process tool_call (same or next response); Vertex often streams turn_complete before tool_call.
                    pending_turn_complete = True
                    if not response.tool_call:
                        seen_tool_calls.clear()
                        _model_spoke_this_turn = False
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
                    # Process add_live_step before capture_step_photo so step
                    # count is correct when the photo's step_number is inferred.
                    _fcs = sorted(
                        response.tool_call.function_calls,
                        key=lambda f: (0 if f.name in ("add_live_step", "add_live_ingredient") else 1 if f.name == "capture_step_photo" else 0),
                    )
                    for fc in _fcs:
                        args = _deep_convert(fc.args) if fc.args else {}
                        dedup_key = (fc.name, json.dumps(args, sort_keys=True))
                        if dedup_key in seen_tool_calls:
                            print(f"[nonna] skipping duplicate tool call: {fc.name}", flush=True)
                            # Still need to ack it so the model isn't left hanging
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue
                        seen_tool_calls.add(dedup_key)
                        print(f"[nonna] tool: {fc.name} args={json.dumps(args)[:120]}", flush=True)

                        if fc.name == "fetch_recipe":
                            # Hard guard: if we're already documenting, do not fetch — prevents double-thread confusion
                            if self.document_mode_started_at is not None or len(self.live_steps) > 0 or len(self.live_ingredients) > 0:
                                print("[nonna] fetch_recipe BLOCKED — already in document mode", flush=True)
                                tool_responses.append(types.FunctionResponse(
                                    name=fc.name,
                                    response={"error": "DOCUMENT_MODE: You are documenting the user's recipe. Do NOT call fetch_recipe. Continue with add_live_step and add_live_ingredient only."},
                                    id=fc.id,
                                ))
                                continue
                            dish = args.get("dish_name", "")
                            await _safe_send(websocket, json.dumps({"type": "recipe_search_start", "query": dish}))
                            try:
                                async with httpx.AsyncClient(timeout=30) as hx:
                                    resp = await hx.post(
                                        f"{RECIPE_AGENT_URL}/parse",
                                        json={"input": dish, "persona": self.persona},
                                    )
                                    resp.raise_for_status()
                                    data = resp.json()
                                self.current_recipe = data["recipe"]
                                await _safe_send(websocket, json.dumps({
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
                                print(f"[nonna] fetch_recipe failed: {err_msg}", flush=True)
                                if body:
                                    print(f"[nonna] response body: {body}", flush=True)
                                await _safe_send(websocket, json.dumps({"type": "recipe_search_failed", "error": err_msg}))
                                tool_responses.append(types.FunctionResponse(
                                    name=fc.name,
                                    response={"error": err_msg},
                                    id=fc.id,
                                ))
                                continue

                        elif fc.name == "start_stopwatch":
                            if self.document_mode_started_at is None:
                                self.document_mode_started_at = time.time()
                                if self._draft_key is None:
                                    self._draft_key = self.document_mode_started_at
                            self.active_stopwatch_label = args.get("label", "Step")
                            # fall through to send tool_call to frontend

                        elif fc.name == "set_draft_name":
                            self.draft_name = (args.get("name") or "").strip() or None
                            if self.draft_name:
                                if self.document_mode_started_at is None:
                                    self.document_mode_started_at = time.time()
                                    if self._draft_key is None:
                                        self._draft_key = self.document_mode_started_at
                                _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                _write_draft(self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc, user_id=self.user_id)
                                await websocket.send_text(json.dumps({"type": "draft_name", "name": self.draft_name}))
                            tool_responses.append(types.FunctionResponse(
                                name=fc.name,
                                response={
                                    "result": "ok",
                                    "reminder": "The name is now set. Immediately prompt the user for ingredients or the first step — e.g. 'What ingredients are you using?' or 'What are you doing first?' Do not stop after naming.",
                                },
                                id=fc.id,
                            ))
                            continue

                        elif fc.name == "add_live_step":
                            if self.document_mode_started_at is None:
                                self.document_mode_started_at = time.time()
                                if self._draft_key is None:
                                    self._draft_key = self.document_mode_started_at
                            step_args = dict(args)
                            if step_args.get("timer_seconds") is not None and step_args["timer_seconds"] <= 0:
                                del step_args["timer_seconds"]
                            # Deduplicate: skip if instruction is identical to the last recorded step
                            _instr = (step_args.get("instruction") or "").strip().lower()
                            if self.live_steps and (self.live_steps[-1].get("instruction") or "").strip().lower() == _instr:
                                step_summary = [f"{j+1}. {s.get('instruction','')}" for j, s in enumerate(self.live_steps)]
                                tool_responses.append(types.FunctionResponse(name=fc.name, response={
                                    "result": "ok",
                                    "note": "duplicate_skipped",
                                    "total_steps": len(self.live_steps),
                                    "all_steps": step_summary,
                                }, id=fc.id))
                                continue
                            position = step_args.pop("position", None)
                            if position is not None and 1 <= int(position) <= len(self.live_steps):
                                self.live_steps.insert(int(position) - 1, step_args)
                                insert_at = int(position)
                            else:
                                self.live_steps.append(step_args)
                                insert_at = len(self.live_steps)
                            _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                            asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc, user_id=self.user_id))
                            await websocket.send_text(json.dumps({
                                "type": "live_step",
                                "step": step_args,
                                "position": insert_at,
                            }))
                            step_summary = [f"{j+1}. {s.get('instruction','')}" for j, s in enumerate(self.live_steps)]
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={
                                "result": "ok",
                                "step_number": insert_at,
                                "total_steps": len(self.live_steps),
                                "all_steps": step_summary,
                                "reminder": f"Say aloud that you recorded this step (e.g. 'Step {insert_at} recorded!' or 'Got it!').",
                            }, id=fc.id))
                            continue

                        elif fc.name == "delete_live_step":
                            step_num = int(args.get("step_number", 0))
                            if 1 <= step_num <= len(self.live_steps):
                                self.live_steps.pop(step_num - 1)
                                _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc, user_id=self.user_id))
                                await websocket.send_text(json.dumps({
                                    "type": "delete_live_step",
                                    "step_number": step_num,
                                }))
                            step_summary = [f"{j+1}. {s.get('instruction','')}" for j, s in enumerate(self.live_steps)]
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={
                                "result": "ok",
                                "total_steps": len(self.live_steps),
                                "all_steps": step_summary,
                            }, id=fc.id))
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
                                asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc, user_id=self.user_id))
                                await websocket.send_text(json.dumps({
                                    "type": "edit_live_step",
                                    "step_number": step_num,
                                    "step": updated,
                                }))
                            step_summary = [f"{j+1}. {s.get('instruction','')}" for j, s in enumerate(self.live_steps)]
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={
                                "result": "ok",
                                "step_number": step_num,
                                "total_steps": len(self.live_steps),
                                "all_steps": step_summary,
                            }, id=fc.id))
                            continue

                        elif fc.name == "add_live_ingredient":
                            if self.document_mode_started_at is None:
                                self.document_mode_started_at = time.time()
                                if self._draft_key is None:
                                    self._draft_key = self.document_mode_started_at
                            ing = {
                                "amount": (args.get("amount") or "").strip(),
                                "item": (args.get("item") or "").strip(),
                                "prep": (args.get("prep") or "").strip(),
                            }
                            if ing["item"]:
                                self.live_ingredients.append(ing)
                                _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc, user_id=self.user_id))
                                await websocket.send_text(json.dumps({
                                    "type": "live_ingredient",
                                    "ingredient": ing,
                                }))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "delete_live_ingredient":
                            idx = int(args.get("index", 0))
                            if 1 <= idx <= len(self.live_ingredients):
                                self.live_ingredients.pop(idx - 1)
                                _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc, user_id=self.user_id))
                                await websocket.send_text(json.dumps({
                                    "type": "delete_live_ingredient",
                                    "index": idx,
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
                                # Update the draft document in place to saved (one doc from draft → saved)
                                draft_id = str(self._draft_key or self.document_mode_started_at or time.time())
                                entry = {
                                    "id": draft_id,
                                    "draft": False,
                                    "saved_at": datetime.now(timezone.utc).isoformat(),
                                    "recipe": recipe,
                                    "photos": self.step_photos or [],
                                }
                                storage.save_entry(self.user_id, entry)
                                self._saved_recipe_name_this_session = name
                                await websocket.send_text(json.dumps({"type": "recipe_saved"}))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "save_recipe_to_library":
                            if self._recipe_already_in_library:
                                tool_responses.append(types.FunctionResponse(
                                    name=fc.name,
                                    response={"result": "already_saved", "message": "This recipe is already in the user's library — no need to save again."},
                                    id=fc.id,
                                ))
                                continue
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
                                    draft_id = str(self._draft_key or self.document_mode_started_at or "")
                                    entry = {
                                        "saved_at": datetime.now(timezone.utc).isoformat(),
                                        "recipe": self.current_recipe,
                                        "photos": self.step_photos or [],
                                        "draft": False,
                                    }
                                    if draft_id:
                                        entry["id"] = draft_id
                                    storage.save_entry(self.user_id, entry)
                                    self._saved_recipe_name_this_session = name
                                await websocket.send_text(json.dumps({"type": "recipe_saved"}))
                                tool_responses.append(types.FunctionResponse(
                                    name=fc.name,
                                    response={"result": "saved", "name": name},
                                    id=fc.id,
                                ))
                            else:
                                print("[nonna] save_recipe_to_library: no recipe to save", flush=True)
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
                                # Update live_ingredients in document mode
                                if self.live_ingredients and 0 <= idx < len(self.live_ingredients):
                                    self.live_ingredients[idx] = ing
                                    _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                    asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc, user_id=self.user_id))
                                await websocket.send_text(json.dumps({"type": "edit_ingredient", "index": idx, "ingredient": ing}))
                            tool_responses.append(types.FunctionResponse(name=fc.name, response={"result": "ok"}, id=fc.id))
                            continue

                        elif fc.name == "edit_step":
                            step_num = args.get("step_number")
                            if step_num is not None:
                                instruction = (args.get("instruction") or "").strip()
                                if not instruction and self.current_recipe:
                                    steps = self.current_recipe.get("steps") or []
                                    if 1 <= step_num <= len(steps):
                                        instruction = (steps[step_num - 1].get("instruction") or "").strip()
                                step = {
                                    "instruction": instruction,
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
                            step_num = args.get("step_number")
                            if step_num is None:
                                in_doc = self.document_mode_started_at is not None or self.live_steps or self.live_ingredients
                                if in_doc:
                                    step_num = max(1, len(self.live_steps))
                                elif self.current_recipe:
                                    completed = sorted(self._completed_step_ids)
                                    step_num = (completed[-1] if completed else 1) if self.current_recipe.get("steps") else 1
                            if self.last_video_frame:
                                self.step_photos.append({"step_id": step_num, "data": self.last_video_frame})
                                self.last_video_frame = None  # free memory; next frame will overwrite when needed
                                print(f"[nonna] photo captured (step={step_num}); total {len(self.step_photos)} photo(s)", flush=True)
                                # Persist photo to draft immediately so it survives if the session ends before the next step
                                if self.live_steps or self.live_ingredients:
                                    _acc = self.draft_accumulated_seconds + (time.time() - self.document_mode_started_at if self.document_mode_started_at else 0)
                                    asyncio.create_task(asyncio.to_thread(_write_draft, self.live_steps, self.live_ingredients, name=self.draft_name, started_at=self._draft_key or self.document_mode_started_at, photos=self.step_photos, accumulated_seconds=_acc, user_id=self.user_id))
                                try:
                                    await websocket.send_text(json.dumps({
                                        "type": "photo_captured",
                                        "step_number": step_num,
                                        "data": self.step_photos[-1]["data"],
                                    }))
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

                        if fc.name == "check_timers":
                            now = time.time()
                            self._active_timers = [t for t in self._active_timers if now - t["started_at"] < t["duration"]]
                            if not self._active_timers:
                                tool_responses.append(types.FunctionResponse(name=fc.name, response={"timers": [], "summary": "No active timers."}, id=fc.id))
                            else:
                                timer_info = []
                                for t in self._active_timers:
                                    remaining = max(0, t["duration"] - int(now - t["started_at"]))
                                    mins, secs = divmod(remaining, 60)
                                    timer_info.append({"label": t["label"], "remaining_seconds": remaining, "display": f"{mins}m {secs}s" if mins else f"{secs}s"})
                                tool_responses.append(types.FunctionResponse(name=fc.name, response={"timers": timer_info}, id=fc.id))
                            continue

                        if fc.name == "set_timer":
                            self._active_timers.append({"label": args.get("label", "Timer"), "duration": args.get("duration_seconds", 0), "started_at": time.time()})
                        elif fc.name == "cancel_timer":
                            cancel_label = (args.get("label") or "").lower()
                            self._active_timers = [t for t in self._active_timers if t["label"].lower() != cancel_label]
                        elif fc.name == "edit_timer":
                            edit_label = (args.get("label") or "").lower()
                            for t in self._active_timers:
                                if t["label"].lower() == edit_label:
                                    t["duration"] = args.get("new_duration_seconds", t["duration"])
                                    t["started_at"] = time.time()
                                    break

                        if fc.name == "complete_step":
                            now = time.time()
                            elapsed = now - self._last_step_completed_at
                            current_turns = self._turns_completed
                            turns_since = current_turns - self._turns_at_last_step
                            # Block only if BOTH: very fast (<3s) AND no model turn has happened since last step.
                            # This prevents double-fires while allowing quick steps after user says "next".
                            if self._last_step_completed_at > 0 and elapsed < 3 and turns_since < 1:
                                print(f"[nonna] BLOCKED complete_step — {elapsed:.0f}s / {turns_since} turns since last", flush=True)
                                tool_responses.append(types.FunctionResponse(
                                    name=fc.name,
                                    response={"error": "BLOCKED: You must wait for the user to explicitly say 'done' or 'next' before completing a step. The user has not spoken yet. Stop and wait silently."},
                                    id=fc.id,
                                ))
                                continue
                            step_num = args.get("step_number")
                            if step_num is not None:
                                self._completed_step_ids.add(int(step_num))
                            self._last_step_completed_at = now
                            self._turns_at_last_step = current_turns
                        elif fc.name == "jump_to_step":
                            step_num = args.get("step_number")
                            if step_num is not None:
                                for i in range(1, int(step_num)):
                                    self._completed_step_ids.add(i)
                            self._last_step_completed_at = time.time()
                            self._turns_at_last_step = self._turns_completed
                        await websocket.send_text(json.dumps({
                            "type": "tool_call",
                            "name": fc.name,
                            "args": args,
                            "call_id": fc.id,
                        }))
                        if fc.name == "complete_step":
                            next_step_text = self._get_step_text(int(args.get("step_number", 0)) + 1)
                            if next_step_text:
                                msg = f"Step marked complete. Now read step {int(args.get('step_number', 0)) + 1} aloud: \"{next_step_text}\" — say it ONCE, then stop."
                            else:
                                msg = "All steps complete! Congratulate the cook."
                            tool_responses.append(types.FunctionResponse(
                                name=fc.name,
                                response={"result": msg},
                                id=fc.id,
                            ))
                        elif fc.name == "jump_to_step":
                            step_num = int(args.get("step_number", 0))
                            step_text = self._get_step_text(step_num)
                            step_timer = self._get_step_timer(step_num)
                            if step_text and step_timer:
                                msg = f"Jumped to step {step_num}. In ONE response: read it aloud (\"{step_text}\") then immediately ask 'Want me to set a timer?' — do not stop between them."
                            elif step_text:
                                msg = f"Jumped to step {step_num}. Read it aloud: \"{step_text}\" — say it ONCE, then stop."
                            elif step_timer:
                                msg = f"Jumped to step {step_num}. Read it aloud, then immediately ask 'Want me to set a timer?' in the same response."
                            else:
                                msg = f"Jumped to step {step_num}. Read it aloud ONCE, then stop."
                            tool_responses.append(types.FunctionResponse(
                                name=fc.name,
                                response={"result": msg},
                                id=fc.id,
                            ))
                        else:
                            tool_responses.append(types.FunctionResponse(
                                name=fc.name,
                                response={"result": "ok"},
                                id=fc.id,
                            ))

                    step_tools = any(
                        fc2.name in ("complete_step", "jump_to_step")
                        for fc2 in response.tool_call.function_calls
                    )
                    model_already_spoke = spoke_in_this_response or _model_spoke_this_turn or bool(sent_text_this_turn)

                    # SILENT scheduling on non-step tools prevents the
                    # model from generating speech about the tool result
                    # (e.g. narrating "Timer set!" twice).  Step tools use
                    # normal scheduling so the model reads the next step.
                    if not step_tools:
                        for tr in tool_responses:
                            tr.scheduling = types.FunctionResponseScheduling.SILENT

                    await session.send_tool_response(
                        function_responses=tool_responses
                    )

                    if model_already_spoke:
                        suppress_until_turn_complete = True
                    if step_tools:
                        # Clear suppress so the model can read the new step.
                        suppress_until_turn_complete = False
                        if model_already_spoke:
                            # Model already spoke in this turn (before the tool call),
                            # so the step was likely already read. Block post-tool
                            # speech to prevent it being read a second time.
                            _step_speech_budget = 0
                        else:
                            # Budget: 2 if the step has a timer (step read + timer question
                            # may come in separate turns), 1 otherwise.
                            _jumped_timer = any(
                                self._get_step_timer(int(fc2.args.get("step_number", 0)))
                                for fc2 in response.tool_call.function_calls
                                if fc2.name == "jump_to_step" and fc2.args
                            )
                            _step_speech_budget = 2 if _jumped_timer else 1
                        _step_guard_expires = time.time() + 6
                    elif _step_speech_budget is None:
                        # Only clear budget if no step guard is active —
                        # a non-step tool (e.g. set_timer) called right
                        # after a step tool must NOT wipe the budget.
                        _step_speech_budget = None
                    sent_text_this_turn.clear()
                    seen_tool_calls.clear()
                    pending_turn_complete = False
                    _model_spoke_this_turn = False
            except Exception as e:
                if "websocket.close" not in str(e) and "websocket.send" not in str(e):
                    print(f"[nonna] send error: {e}", flush=True)
                return

    def _build_reconnect_context(self, recipe_text: str | None) -> str:
        """Build a text summary for re-injecting into a fresh Live API session after reconnect."""
        parts = []

        p = PERSONAS.get(self.persona, PERSONAS["nonna"])
        parts.append(f"REMINDER — stay in character at all times:\n{p['base']}")

        parts.append("\nYou are continuing an ongoing cooking session. The user does NOT know there was any interruption "
                      "and must NOT find out. Do NOT say 'I'm back', 'sorry about that', 'where were we', or acknowledge "
                      "any disconnect in any way. Do NOT greet them again. Do NOT ask what they want to cook. "
                      "Just seamlessly continue as if nothing happened — wait silently for the user to speak.")

        in_document_mode = self.document_mode_started_at is not None or self.live_steps or self.live_ingredients

        if in_document_mode:
            parts.append("\n=== YOU ARE IN DOCUMENT MODE === "
                         "You are documenting the user's recipe as they cook. Do NOT call fetch_recipe. "
                         "Only use add_live_step, add_live_ingredient, edit_live_step, delete_live_step, "
                         "set_draft_name, capture_step_photo, finalize_live_recipe.")
            if self.draft_name:
                parts.append(f"\nRecipe name: {self.draft_name}")
            if self.live_ingredients:
                ing_strs = [f"{i.get('amount', '')} {i.get('item', '')}".strip() or i.get("item", "") for i in self.live_ingredients]
                parts.append(f"\nIngredients recorded so far: {'; '.join(ing_strs)}")
            if self.live_steps:
                step_strs = [f"  {j+1}. {s.get('instruction', '')}" for j, s in enumerate(self.live_steps)]
                parts.append(f"\nSteps recorded so far:\n" + "\n".join(step_strs))
            parts.append("\nDo NOT re-add these ingredients or steps. Only add NEW ones the user mentions going forward.")
        elif recipe_text:
            parts.append(f"\n--- RECIPE (already displayed on user's screen) ---\n{recipe_text}\n--- END RECIPE ---")

            if self._completed_step_ids:
                sorted_ids = sorted(self._completed_step_ids)
                parts.append(f"\nSteps already completed: {', '.join(str(s) for s in sorted_ids)}.")
                next_step = max(sorted_ids) + 1
                parts.append(f"The user is currently working on step {next_step}. Wait for them to say 'done' or 'next' before advancing.")
            else:
                parts.append("\nNo steps completed yet. Wait for the user to tell you they are ready or to say 'done'/'next'.")
        else:
            parts.append("\nNo recipe is loaded and no documenting has started. Wait for the user to speak — they will tell you what they want to do.")

        recent = self._transcript_log[-20:]
        if recent:
            convo_lines = []
            for entry in recent:
                prefix = "You said" if entry["role"] == "assistant" else "User said"
                convo_lines.append(f"  {prefix}: {entry['text'][:200]}")
            parts.append("\nRecent conversation for context:\n" + "\n".join(convo_lines))

        now = time.time()
        live_timers = [t for t in self._active_timers if now - t["started_at"] < t["duration"]]
        if live_timers:
            timer_lines = []
            for t in live_timers:
                remaining = max(0, t["duration"] - int(now - t["started_at"]))
                mins, secs = divmod(remaining, 60)
                timer_lines.append(f"  - {t['label']}: ~{mins}m {secs}s remaining")
            parts.append("\nActive timers:\n" + "\n".join(timer_lines))

        parts.append("\nCRITICAL: Say NOTHING proactively. Do NOT greet, do NOT ask questions, do NOT summarize. "
                      "Wait in silence for the user to speak first, then respond naturally.")
        return "\n".join(parts)

    async def close(self):
        pass
