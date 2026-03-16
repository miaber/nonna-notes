import asyncio
import json
import os
import re

from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

import cache as recipe_cache
from recipe_fetcher import fetch_recipe, is_url, _youtube_video_id
from schema import RecipeSchema


def _normalize_url_for_cache(url: str) -> str:
    """Stable cache key for a recipe URL. YouTube URLs keyed by video ID."""
    url = url.strip()
    video_id = _youtube_video_id(url)
    if video_id:
        return f"yt:{video_id}"
    return url.lower().rstrip("/")

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

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
      "visual_checkpoint": boolean — true if cook needs to assess doneness visually,
      "image": "string URL or null — step photo from the original recipe, if available"
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

SEARCH_PROMPT = """Search the web for a recipe for "{query}" from a well-known cooking website (AllRecipes, BBC Good Food, Food Network, Serious Eats, Tasty, NYT Cooking, Epicurious, etc.).

Copy the complete recipe from the search results — ingredients, amounts, and full step-by-step instructions. Do not use your own knowledge or make anything up.

At the end, include the source URL on its own line prefixed with "SOURCE:" (e.g. SOURCE: https://www.seriouseats.com/...)

Format as plain text with clear sections for Ingredients and Instructions."""

GENERATE_PROMPT = f"""Generate a complete, accurate recipe for "{{query}}" in the style of {{persona_hint}}.
Return ONLY valid JSON matching this exact schema. No markdown fences, no explanation.

Schema:
{SCHEMA_DESCRIPTION}

Prefer more, shorter steps over fewer long ones. One main action per step where possible. Do not combine multiple actions into a single long step.
Include realistic timer_seconds for timed steps. Set visual_checkpoint true for steps requiring visual assessment."""

IMAGE_PARSE_PROMPT = f"""You are looking at one or more images of a recipe. This could be a photo of a cookbook page, a handwritten recipe card, a screenshot of a recipe, a magazine clipping, etc.

Extract the complete recipe from the image(s) and return it as valid JSON matching this exact schema. No markdown fences, no explanation, no extra text.

Schema:
{SCHEMA_DESCRIPTION}

Rules:
- Transcribe ingredients and steps faithfully from the image
- If amounts are hard to read, make your best guess and note uncertainty in a tip
- Prefer more, shorter steps — one main action per step where possible
- Set timer_seconds (integer) for any timed step (boiling, roasting, frying, resting, etc.)
- Set visual_checkpoint: true when cook must assess doneness visually
- If the image is not a recipe or is unreadable, return a JSON object with just {{"error": "description of the problem"}}"""


def _apply_step_images(recipe: RecipeSchema, step_images: dict[int, str]):
    """Attach per-step images from JSON-LD to the parsed recipe.

    step_images maps the original 1-based step index (from the source page) to
    an image URL. Gemini may reorder or split steps, so we do a best-effort
    match: if the step count matches, use direct index mapping. Otherwise, only
    fill in images for steps whose id exists in the map.
    """
    if not step_images:
        return
    source_count = max(step_images.keys()) if step_images else 0
    parsed_count = len(recipe.steps)
    if parsed_count == source_count:
        for step in recipe.steps:
            url = step_images.get(step.id)
            if url and not step.image:
                step.image = url
    else:
        for step in recipe.steps:
            url = step_images.get(step.id)
            if url and not step.image:
                step.image = url
        # If Gemini split steps (more parsed than source), try to fill gaps
        # by carrying forward the nearest earlier step's image
        if parsed_count > source_count:
            last_img = None
            for step in recipe.steps:
                if step.image:
                    last_img = step.image
                elif last_img and not step.image:
                    step.image = last_img
    assigned = sum(1 for s in recipe.steps if s.image)
    if assigned:
        print(f"[recipe-agent] attached {assigned}/{len(recipe.steps)} step image(s)", flush=True)


async def _match_content_images_to_steps(recipe: RecipeSchema, content_images: list[dict]):
    """Use Gemini vision to match blog post images to recipe steps.

    Downloads the actual images and sends them to Gemini alongside each image's
    surrounding HTML context (alt text, preceding paragraph) and the full list
    of step instructions. Gemini sees the photos and understands what cooking
    action each one depicts, producing far more accurate matches than keywords.
    """
    if not content_images or not recipe.steps:
        return

    import httpx

    # Download images concurrently (cap at 30 to limit bandwidth/tokens)
    candidates = content_images[:30]
    image_parts = []
    image_descriptions = []

    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as http:
        async def _fetch_img(idx: int, ci: dict):
            try:
                resp = await http.get(ci["url"])
                if resp.status_code != 200 or len(resp.content) < 1000:
                    return None
                ct = resp.headers.get("content-type", "image/jpeg")
                mime = ct.split(";")[0].strip()
                if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                    mime = "image/jpeg"
                alt = (ci.get("alt") or "").strip()
                ctx = (ci.get("context") or "").strip()
                desc = f"Image {idx}: "
                if alt:
                    desc += f'alt="{alt}" '
                if ctx:
                    desc += f'preceding text: "{ctx[:200]}"'
                return (idx, resp.content, mime, desc.strip())
            except Exception:
                return None

        results = await asyncio.gather(*[_fetch_img(i, ci) for i, ci in enumerate(candidates)])

    for r in results:
        if r is None:
            continue
        idx, data, mime, desc = r
        image_parts.append((idx, types.Part.from_bytes(data=data, mime_type=mime), desc))

    if not image_parts:
        return

    steps_text = "\n".join(f"Step {s.id}: {s.instruction}" for s in recipe.steps)

    # Build multimodal prompt: images interleaved with their text context
    parts: list[types.Part | str] = []
    parts.append(
        "You are matching recipe photos to recipe steps.\n\n"
        "Below are photos from a recipe blog post, each with its surrounding text context "
        "(alt text and the paragraph before it). After the photos, you'll see the recipe steps.\n\n"
        "For each image, decide which step it best illustrates. An image may match no step "
        "(e.g. hero/beauty shots of the finished dish, ingredient flat-lays, or unrelated photos).\n\n"
        "IMPORTANT about paired images: Recipe blogs often place images in PAIRS between "
        "paragraphs. Each image in a pair typically shows a DIFFERENT cooking action or stage. "
        "Look carefully at the actual contents of each photo (what is in the bowl, the texture, "
        "the color, what action is being performed) to match it to the correct step. "
        "Do NOT assume adjacent images belong to the same step.\n\n"
        "A COLLAGE image (two photos combined side-by-side in ONE file) showing two distinct "
        "stages may be assigned to TWO consecutive steps.\n\n"
        "Rules:\n"
        "- Only match process/action photos, not finished dish glamour shots or ingredient flat-lays\n"
        "- Each step should get at most one image\n"
        "- Collage images can map to multiple steps via the \"steps\" array\n"
        "- Look at WHAT is happening in each photo, not just the setting or angle\n"
        "- Assignments must respect page order: if image 3 is matched to step 2, image 5 can only match step 2 or later\n"
        "- It is better to leave a step unmatched than to assign a wrong image\n\n"
        "Photos:\n"
    )
    for idx, img_part, desc in image_parts:
        parts.append(f"\n{desc}\n")
        parts.append(img_part)

    parts.append(f"\n\nRecipe steps:\n{steps_text}\n\n"
                 "Return ONLY a JSON array of objects: [{\"image\": <image_index>, \"steps\": [<step_id>, ...]}, ...]\n"
                 "Each entry's \"steps\" array contains 1 step ID, or 2 consecutive step IDs for collage images.\n"
                 "Only include matches you're confident about. Return [] if no good matches exist.")

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=parts,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = _extract_text(response).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        matches = json.loads(raw)
        if not isinstance(matches, list):
            matches = []
    except Exception as e:
        print(f"[recipe-agent] vision matching failed: {e}", flush=True)
        return

    # Build URL lookup from image index
    url_by_idx = {i: candidates[i]["url"] for i in range(len(candidates))}
    step_by_id = {s.id: s for s in recipe.steps}

    assigned = 0
    for m in matches:
        img_idx = m.get("image")
        if img_idx is None:
            continue
        url = url_by_idx.get(img_idx)
        if not url:
            continue
        step_ids = m.get("steps") or []
        if not step_ids:
            single = m.get("step")
            if single is not None:
                step_ids = [single]
        for step_id in step_ids:
            step = step_by_id.get(step_id)
            if step and not step.image:
                step.image = url
                assigned += 1

    if assigned:
        print(f"[recipe-agent] Gemini vision matched {assigned}/{len(recipe.steps)} step(s) to images", flush=True)


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
            raw_text, images, step_images, content_images = await fetch_recipe(input_text)
            prompt = PARSE_PROMPT.replace("{recipe_text}", raw_text)
            recipe, source = await _call_gemini(prompt, "url")
            recipe.source_url = input_text.strip()
            recipe.source_images = images
            _apply_step_images(recipe, step_images)
            if content_images:
                await _match_content_images_to_steps(recipe, content_images)
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
        result = await _search_and_parse_recipe(url_query, source_url=input_text.strip())
        if result:
            return result
        raise ValueError(f"Could not fetch recipe from {input_text}")

    result = await _search_and_parse_recipe(input_text)
    if result:
        return result

    raise ValueError(
        "Couldn't find a recipe from the web for that. Try pasting a recipe URL, or a more specific search."
    )


async def _search_and_parse_recipe(query: str, source_url: str | None = None) -> tuple[RecipeSchema, str] | None:
    """
    Two-pass grounding approach:
      1. Plain-text call with Google Search grounding → recipe text + source URLs
         (grounding_chunks is only populated for plain text, not JSON output)
      2. Parse the text into structured JSON via _call_gemini

    Returns None if grounding fails or returns unusable content.
    """
    # --- Pass 1: search + get plain text (grounding URLs only work without JSON mode) ---
    prompt = SEARCH_PROMPT.replace("{query}", query)
    response = None

    for try_i in range(3):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[
                        types.Tool(google_search=types.GoogleSearch())
                    ],
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

    print(f"[recipe-agent] grounding raw text ({len(raw)} chars): {raw[:200]}…", flush=True)

    # --- Extract source URL from grounding metadata ---
    grounding_url = None
    try:
        cand = response.candidates[0] if response.candidates else None
        meta = getattr(cand, "grounding_metadata", None) if cand else None
        chunks = getattr(meta, "grounding_chunks", None) or []
        print(f"[recipe-agent] grounding_chunks ({len(chunks)})", flush=True)
        urls = [getattr(getattr(ch, "web", None), "uri", None) for ch in chunks]
        urls = [u for u in urls if u]
        if urls:
            grounding_url = await _resolve_redirect(urls[0])
            print(f"[recipe-agent] grounding source: {grounding_url}", flush=True)
        else:
            # Try search_entry_point HTML
            raw_redirect = _extract_recipe_url_from_entry_point(meta) if meta else None
            if raw_redirect:
                grounding_url = await _resolve_redirect(raw_redirect)
                print(f"[recipe-agent] grounding entry_point source: {grounding_url}", flush=True)
    except Exception as _ge:
        print(f"[recipe-agent] grounding metadata error: {_ge}", flush=True)

    # Also check if model included a SOURCE: line in the text
    if not grounding_url or "google.com" in (grounding_url or ""):
        m = re.search(r"SOURCE:\s*(https?://\S+)", raw, re.I)
        if m:
            grounding_url = m.group(1).rstrip(".,)")
            print(f"[recipe-agent] source from text: {grounding_url}", flush=True)

    # --- Pass 2: parse the plain text into structured JSON ---
    parse_prompt = PARSE_PROMPT.replace("{recipe_text}", raw)
    try:
        recipe, _ = await _call_gemini(parse_prompt, "url")
    except Exception as e:
        print(f"[recipe-agent] pass 2 parse failed: {e}", flush=True)
        return None

    source = "url" if grounding_url and "google.com" not in grounding_url else "generated"
    recipe.source_url = source_url or grounding_url

    # --- Fetch images from the source page (if we have a real URL) ---
    if recipe.source_url and "google.com" not in recipe.source_url:
        try:
            _, images, step_images, content_images = await fetch_recipe(recipe.source_url)
            recipe.source_images = images
            _apply_step_images(recipe, step_images)
            if content_images:
                await _match_content_images_to_steps(recipe, content_images)
        except Exception as img_err:
            print(f"[recipe-agent] image fetch from grounding URL failed: {img_err}", flush=True)

    print(f"[recipe-agent] grounding recipe: '{recipe.name}' ({len(recipe.ingredients)} ingredients, {len(recipe.steps)} steps, source={source}, url={recipe.source_url})", flush=True)
    return recipe, source


def _extract_recipe_url_from_entry_point(meta) -> str | None:
    """Extract a recipe URL from search_entry_point rendered HTML.

    Tries vertexaisearch redirect URLs first, then falls back to any
    href pointing to a known recipe site.
    """
    try:
        entry = getattr(meta, "search_entry_point", None)
        if not entry:
            return None
        rendered = getattr(entry, "rendered_content", None) or ""
        if not rendered:
            return None
        # Extract ALL hrefs from the rendered HTML
        all_hrefs = re.findall(r'href=["\'](https?://[^"\']+)["\']', rendered)
        print(f"[recipe-agent] search_entry_point hrefs: {all_hrefs}", flush=True)
        # 1. Prefer direct recipe-site URLs (skip vertexaisearch redirects — they
        #    resolve to google.com/search on the Gemini API)
        for href in all_hrefs:
            if _is_acceptable_recipe_url(href):
                print(f"[recipe-agent] found direct recipe URL in entry_point: {href[:80]}", flush=True)
                return href
        # 2. Fall back to vertexaisearch redirect URLs
        for href in all_hrefs:
            if "vertexaisearch.cloud.google.com/grounding-api-redirect/" in href:
                return href
        return None
    except Exception:
        return None


# Hosts that are not recipe pages — reject these when resolving grounding redirects
_NON_RECIPE_HOSTS = frozenset(
    h.lower()
    for h in (
        "google.com",
        "www.google.com",
        "vertexaisearch.cloud.google.com",
        "accounts.google.com",
        "support.google.com",
        "www.bing.com",
        "bing.com",
        "duckduckgo.com",
    )
)


def _is_acceptable_recipe_url(url: str) -> bool:
    """True if the URL looks like a recipe page, not a search engine or redirect hub."""
    if not url or not url.strip():
        return False
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url.strip())
        host = (parsed.netloc or "").lower().lstrip("www.")
        if not host:
            return False
        if host in _NON_RECIPE_HOSTS:
            return False
        if "google." in host or "google " in host:
            return False
        return True
    except Exception:
        return False


async def _resolve_redirect(url: str) -> str:
    """Follow redirects to get the final URL (e.g. vertexaisearch.cloud.google.com → actual recipe page)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            resp = await client.head(url)
            return str(resp.url)
    except Exception:
        return url


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


_SUPPORTED_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "image/heif", "image/heic",
})


async def parse_recipe_images(images: list[tuple[bytes, str]], persona: str = "nonna") -> tuple[RecipeSchema, str]:
    """Parse a recipe from one or more uploaded images using Gemini multimodal.

    images is a list of (bytes, mime_type) tuples — one per page/photo.
    """
    # Build contents: all images first, then the text prompt (per Gemini docs)
    contents: list = []
    for i, (image_bytes, mime_type) in enumerate(images):
        if mime_type not in _SUPPORTED_IMAGE_TYPES:
            raise ValueError(f"Unsupported image type: {mime_type}")
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
    contents.append(IMAGE_PARSE_PROMPT)

    print(f"[recipe-agent] parsing recipe from {len(images)} image(s)", flush=True)

    for try_i in range(3):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
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
                    raise
                if try_i < 2:
                    delay = _parse_retry_delay(e)
                    print(f"[recipe-agent] image parse rate limited, retrying in {delay:.0f}s…", flush=True)
                    await asyncio.sleep(delay)
                    continue
            raise

    raw_text = _extract_text(response).strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    data = json.loads(raw_text)
    if "error" in data and len(data) == 1:
        raise ValueError(f"Could not parse recipe from image: {data['error']}")

    recipe = RecipeSchema.model_validate(data)
    import base64
    for image_bytes, mime_type in images:
        b64 = base64.b64encode(image_bytes).decode()
        recipe.source_images.append(f"data:{mime_type};base64,{b64}")
    print(f"[recipe-agent] image recipe: '{recipe.name}' ({len(recipe.ingredients)} ingredients, {len(recipe.steps)} steps, {len(recipe.source_images)} source image(s))", flush=True)
    return recipe, "image"


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

    raw_text = _extract_text(response).strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

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
            fix_prompt = (
                f"The following JSON is invalid or doesn't match the required schema. "
                f"Fix it and return ONLY valid JSON:\n\n{raw_text}\n\nError: {e}"
            )
            return await _call_gemini(fix_prompt, source, attempt=1)
        raise ValueError(f"Recipe parsing failed after retry: {e}\nRaw: {raw_text[:300]}")
