import os
import json
import re
import asyncio

from recipe_fetcher import fetch_recipe, is_url, _youtube_video_id
from schema import RecipeSchema
from google import genai
import cache as recipe_cache


def _normalize_url_for_cache(url: str) -> str:
    """Stable cache key for a recipe URL. YouTube URLs keyed by video ID."""
    url = url.strip()
    video_id = _youtube_video_id(url)
    if video_id:
        return f"yt:{video_id}"
    return url.lower().rstrip("/")


from google.genai import types
from google.genai import errors
from dotenv import load_dotenv

load_dotenv()

_USE_VERTEX = os.getenv("USE_VERTEX_AI", "").strip().lower() in ("1", "true", "yes")
_GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("FIREBASE_PROJECT_ID", "")
_GCP_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")

if _USE_VERTEX:
    client = genai.Client(vertexai=True, project=_GCP_PROJECT, location=_GCP_LOCATION)
else:
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

MODEL = "gemini-2.5-flash"

SCHEMA_DESCRIPTION = """{
  "name": "string — recipe name",
  "description": "string — 1-2 sentence description",
  "servings": integer,
  "total_time_minutes": integer or null,
  "ingredients": [
    {"amount": "string e.g. '200g' or '2'", "item": "string e.g. 'spaghetti'", "prep": "string e.g. 'finely diced' or ''"}
  ],
  "steps": [
    {
      "id": integer starting at 1,
      "instruction": "string — full step instruction",
      "timer_seconds": integer or null,
      "visual_checkpoint": boolean — true if cook needs to assess doneness visually
    }
  ],
  "tips": ["string — optional tips array, can be empty"]
}"""

PARSE_PROMPT = f"""Parse the following recipe text into this exact JSON schema.
Return ONLY valid JSON. No markdown fences, no explanation, no extra text.

Schema:
{SCHEMA_DESCRIPTION}

Prefer more, shorter steps over fewer long ones. Break combined instructions into separate steps (one main action per step where possible). Avoid packing multiple actions into a single step.
Set timer_seconds to a non-null integer for steps that involve timed cooking (boiling, roasting, frying, resting, etc.).
Set visual_checkpoint to true for steps where the cook needs to check for doneness or a specific visual state.
If the text is from a video transcript or description, extract ingredients and steps from the speaker's instructions and ignore filler words (um, like, so, etc.).

Recipe text:
{{recipe_text}}"""

# Used for the grounded search+parse call — no JSON mode so we extract from text
SEARCH_AND_PARSE_PROMPT = f"""Search the web for a recipe for "{{query}}" from a well-known cooking website (AllRecipes, BBC Good Food, Food Network, Serious Eats, Tasty, NYT Cooking, Epicurious, etc.).

Read the actual recipe from the search results — copy the real ingredients and steps, do not use your own knowledge or make anything up.

Return ONLY valid JSON matching this schema. No markdown fences, no explanation:
{SCHEMA_DESCRIPTION}

Rules:
- Copy ingredients and steps from the real recipe you found in search results
- Prefer more, shorter steps — one main action per step
- Set timer_seconds (integer) for any timed step (boiling, roasting, frying, resting, etc.)
- Set visual_checkpoint: true when cook must assess doneness visually"""

GENERATE_PROMPT = f"""Generate a complete, accurate recipe for "{{query}}" in the style of {{persona_hint}}.
Return ONLY valid JSON matching this exact schema. No markdown fences, no explanation.

Schema:
{SCHEMA_DESCRIPTION}

Prefer more, shorter steps over fewer long ones. One main action per step where possible. Do not combine multiple actions into a single long step.
Include realistic timer_seconds for timed steps. Set visual_checkpoint true for steps requiring visual assessment."""


def _mock_recipe(query: str) -> tuple[RecipeSchema, str]:
    """Return a fixture recipe without calling the API. Use RECIPE_AGENT_MOCK=1 to enable."""
    return (
        RecipeSchema(
            name=query or "Test Recipe",
            description="A mock recipe for testing without API calls.",
            servings=4,
            total_time_minutes=30,
            ingredients=[
                {"amount": "2 cups", "item": "flour", "prep": ""},
                {"amount": "1/2 cup", "item": "butter", "prep": "softened"},
                {"amount": "1", "item": "egg", "prep": ""},
                {"amount": "1 tsp", "item": "vanilla", "prep": ""},
            ],
            steps=[
                {"id": 1, "instruction": "Preheat oven to 350°F (175°C).", "timer_seconds": None, "visual_checkpoint": False},
                {"id": 2, "instruction": "Mix dry ingredients in a bowl.", "timer_seconds": None, "visual_checkpoint": False},
                {"id": 3, "instruction": "Add wet ingredients and mix until combined.", "timer_seconds": None, "visual_checkpoint": False},
                {"id": 4, "instruction": "Bake until golden, about 15–20 minutes.", "timer_seconds": 900, "visual_checkpoint": True},
            ],
            tips=["Let cool before serving."],
        ),
        "mock",
    )


async def parse_recipe(input_text: str, persona: str = "nonna") -> tuple[RecipeSchema, str]:
    """
    Parse a URL or recipe name/description into a structured RecipeSchema.
    Returns (recipe, source) where source is "url", "generated", or "mock".

    For URL input: fetches and parses the page directly.
    For text input: uses Gemini with Google Search grounding to find and extract
    a real recipe from the web in one call (avoids scraping 403s). Falls back to
    generation only if grounding finds nothing usable.
    """
    if os.getenv("RECIPE_AGENT_MOCK", "").lower() in ("1", "true", "yes"):
        print("[recipe-agent] mock mode: returning fixture recipe", flush=True)
        return _mock_recipe(input_text.strip() or "Test Recipe")

    persona_hint = "Gordon Ramsay — precise, direct, professional" if persona == "gordon" else "an Italian grandmother — warm, traditional, thorough"

    if is_url(input_text):
        cache_key = _normalize_url_for_cache(input_text)
        entry = None
        try:
            entry = recipe_cache.get(cache_key)
        except Exception as e:
            print(f"[recipe-agent] cache get error (treating as miss): {e}", flush=True)
        if entry:
            print(f"[recipe-agent] cache hit: {cache_key[:50]}...", flush=True)
            return (RecipeSchema.model_validate(entry["recipe"]), entry["source"])

        try:
            raw_text = await fetch_recipe(input_text)
            prompt = PARSE_PROMPT.replace("{recipe_text}", raw_text)
            recipe, source = await _call_gemini(prompt, "url")
            try:
                recipe_cache.set(cache_key, recipe.model_dump(), source)
            except Exception as e:
                print(f"[recipe-agent] cache set error (recipe still returned): {e}", flush=True)
            return (recipe, source)
        except Exception as e:
            print(f"[recipe-agent] direct URL fetch failed ({e}) — retrying via grounding", flush=True)
        # Scraping failed (403, JS-rendered, etc.) — extract a dish name from the URL
        # path and use that as the search query (avoids passing the full URL as a query)
        from urllib.parse import urlparse
        path_parts = [p for p in urlparse(input_text).path.replace("-", " ").split("/") if len(p) > 4]
        url_query = path_parts[-1] if path_parts else input_text
        result = await _search_and_parse_recipe(url_query)
        if result:
            return result
        raise ValueError(f"Could not fetch recipe from {input_text}")

    # Primary: search + parse via grounding in one call
    result = await _search_and_parse_recipe(input_text)
    if result:
        return result

    # Fallback: generate from Gemini's own knowledge
    print("[recipe-agent] grounding failed — generating recipe from model knowledge", flush=True)
    prompt = GENERATE_PROMPT.replace("{query}", input_text).replace("{persona_hint}", persona_hint)
    return await _call_gemini(prompt, "generated")


async def _search_and_parse_recipe(query: str) -> tuple[RecipeSchema, str] | None:
    """
    Single Gemini call with Google Search grounding: finds a real recipe on the web
    and returns it as a parsed RecipeSchema. No URL fetching — Gemini reads the content
    via grounding, bypassing 403s from scrapers.

    Returns None if grounding fails or returns unusable content (caller falls back to generation).
    """
    prompt = SEARCH_AND_PARSE_PROMPT.replace("{query}", query)
    response = None

    for try_i in range(3):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            break
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429:
                msg = str(e).strip() or repr(e)
                print(f"[recipe-agent] search rate limited (429): {msg}", flush=True)
                if "PerDay" in msg or "per day" in msg.lower() or "free_tier" in msg.lower():
                    print("[recipe-agent] daily quota exceeded — skipping search", flush=True)
                    return None
                delay = _parse_retry_delay(e)
                if try_i < 2 and delay <= 65:
                    print(f"[recipe-agent] retrying in {delay:.0f}s…", flush=True)
                    await asyncio.sleep(delay)
                    continue
                return None
            print(f"[recipe-agent] grounding search failed: {e}", flush=True)
            return None

    if response is None:
        return None

    raw = _extract_text(response).strip()
    if not raw:
        print("[recipe-agent] grounding returned empty response", flush=True)
        return None

    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    # Extract the JSON object
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        print("[recipe-agent] grounding response contained no JSON", flush=True)
        return None

    json_text = raw[start:end + 1]

    try:
        recipe = RecipeSchema.model_validate_json(json_text)
    except Exception as e:
        print(f"[recipe-agent] grounding JSON parse failed: {e} — raw: {json_text[:200]}", flush=True)
        return None

    # Determine if grounding actually found a real page
    source = "generated"
    try:
        cand = response.candidates[0] if response.candidates else None
        meta = getattr(cand, "grounding_metadata", None) if cand else None
        chunks = getattr(meta, "grounding_chunks", None) or []
        urls = [getattr(getattr(ch, "web", None), "uri", None) for ch in chunks]
        urls = [u for u in urls if u]
        if urls:
            source = "url"
            print(f"[recipe-agent] grounding found {len(urls)} source(s): {urls[0]}", flush=True)
        else:
            print("[recipe-agent] grounding returned no source URLs — recipe may be from model knowledge", flush=True)
    except Exception:
        pass

    print(f"[recipe-agent] grounding recipe: '{recipe.name}' ({len(recipe.ingredients)} ingredients, {len(recipe.steps)} steps, source={source})", flush=True)
    return recipe, source


def _extract_text(response) -> str:
    """Return only non-thought text parts (handles gemini-2.5 thinking models)."""
    try:
        parts = response.candidates[0].content.parts or []
        texts = [p.text for p in parts if p.text and not getattr(p, "thought", False)]
        if texts:
            return "".join(texts)
    except Exception:
        pass
    return response.text or ""


def _parse_retry_delay(err: Exception) -> float:
    """Extract retry delay from 429 error message (e.g. 'Please retry in 27.4s')."""
    msg = str(err)
    m = re.search(r"retry in ([\d.]+)s", msg, re.I)
    return float(m.group(1)) if m else 30.0


async def _call_gemini(prompt: str, source: str, attempt: int = 0) -> tuple[RecipeSchema, str]:
    for try_i in range(3):  # Up to 3 attempts for rate limits
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            break
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429:
                msg = str(e)
                if "PerDay" in msg or "per day" in msg.lower() or "free_tier" in msg.lower():
                    print(f"[recipe-agent] daily quota exceeded — not retrying", flush=True)
                    raise
                if try_i < 2:
                    delay = _parse_retry_delay(e)
                    print(f"[recipe-agent] rate limited, retrying in {delay:.0f}s…", flush=True)
                    await asyncio.sleep(delay)
                    continue
            raise

    raw_text = _extract_text(response)
    # Strip accidental markdown fences
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1]
        raw_text = raw_text.rsplit("```", 1)[0].strip()

    # If we got thought fragments (e.g. '\n  "name"'), try to extract JSON from full response
    if not raw_text.strip().startswith("{"):
        fallback = response.text or ""
        start = fallback.find("{")
        end = fallback.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw_text = fallback[start : end + 1]
    try:
        recipe = RecipeSchema.model_validate_json(raw_text)
        return recipe, source
    except Exception as e:
        print(f"[recipe-agent] parse failed: raw_text={repr(raw_text[:200])} response.text={repr((response.text or '')[:200])}", flush=True)
        if attempt == 0:
            # One retry with stricter prompt
            fix_prompt = (
                f"The following JSON is invalid or doesn't match the required schema. "
                f"Fix it and return ONLY valid JSON:\n\n{raw_text}\n\nError: {e}"
            )
            return await _call_gemini(fix_prompt, source, attempt=1)
        raise ValueError(f"Recipe parsing failed after retry: {e}\nRaw: {raw_text[:300]}")
