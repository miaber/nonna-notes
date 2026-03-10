import asyncio
import json
import re
import os
import httpx
from bs4 import BeautifulSoup

MAX_CHARS = 4000


def _youtube_video_id(url: str) -> str | None:
    """Extract YouTube video ID from common URL forms. Returns None if not a YouTube URL."""
    url = url.strip()
    if "youtube.com" in url or "youtu.be" in url:
        m = re.search(r"youtu\.be/([a-zA-Z0-9_-]{11})", url)
        if m:
            return m.group(1)
        m = re.search(r"(?:v=|embed/|v/)([a-zA-Z0-9_-]{11})", url)
        if m:
            return m.group(1)
    return None


def _fetch_youtube_transcript_sync(video_id: str) -> str:
    """Fetch transcript for a YouTube video (sync). Returns concatenated text. Raises on failure.

    Uses proxy if set: YOUTUBE_TRANSCRIPT_PROXY (http://host:port or http://user:pass@host:port)
    or Webshare rotating residential via YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME + YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD.
    Proxy is required when running on cloud (e.g. Cloud Run) as YouTube blocks datacenter IPs.
    """
    from youtube_transcript_api import YouTubeTranscriptApi

    proxy_config = None
    proxy_url = os.getenv("YOUTUBE_TRANSCRIPT_PROXY", "").strip()
    ws_user = os.getenv("YOUTUBE_TRANSCRIPT_WEBSHARE_USERNAME", "").strip()
    ws_pass = os.getenv("YOUTUBE_TRANSCRIPT_WEBSHARE_PASSWORD", "").strip()

    if ws_user and ws_pass:
        try:
            from youtube_transcript_api.proxies import WebshareProxyConfig
            proxy_config = WebshareProxyConfig(proxy_username=ws_user, proxy_password=ws_pass)
        except ImportError:
            pass
    elif proxy_url:
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig
            # Same URL for HTTP and HTTPS; library uses it for YouTube (HTTPS)
            proxy_config = GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
        except ImportError:
            pass

    api = YouTubeTranscriptApi(proxy_config=proxy_config) if proxy_config else YouTubeTranscriptApi()
    fetched = api.fetch(video_id)
    raw = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else list(fetched)
    parts = [entry["text"].strip() for entry in raw if entry.get("text")]
    return "\n".join(parts) if parts else ""


async def _fetch_transcript_via_dev_api(video_id: str, api_key: str) -> str | None:
    """Fetch transcript via youtubetranscript.dev API. Works from Cloud Run (no IP block).
    Free tier: 100 extractions/month. Returns transcript text or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://www.youtubetranscript.dev/api/v2/transcribe",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"video": video_id, "language": "en"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            if data.get("status") != "completed" or not data.get("data"):
                return None
            transcript = data["data"].get("transcript") or {}
            text = (transcript.get("text") or "").strip()
            return text if text else None
    except Exception:
        return None


async def _youtube_description(video_id: str) -> tuple[str, str]:
    """Fetch video title and description via YouTube Data API v3. Returns (title, description)."""
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        return ("", "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "snippet", "id": video_id, "key": api_key},
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            if not items:
                return ("", "")
            snippet = items[0].get("snippet", {})
            title = snippet.get("title", "").strip()
            description = snippet.get("description", "").strip()
            return (title, description)
    except Exception:
        return ("", "")




def _extract_json_ld_recipe(soup: BeautifulSoup) -> str | None:
    """Try to pull structured recipe data from JSON-LD (schema.org).

    Almost every recipe site embeds this.  It gives us *just* the recipe
    (name, ingredients, instructions, 
    times) in a few hundred chars instead
    of 6 000 chars of page junk.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        # The JSON-LD can be a single object or a list
        items = data if isinstance(data, list) else [data]

        # It might be nested inside a @graph
        expanded = []
        for item in items:
            if "@graph" in item:
                expanded.extend(item["@graph"])
            else:
                expanded.append(item)

        for item in expanded:
            if not isinstance(item, dict):
                continue
            schema_type = item.get("@type", "")
            # @type can be a string or a list
            types = schema_type if isinstance(schema_type, list) else [schema_type]
            if "Recipe" not in types:
                continue

            # ── Found a Recipe object — format it cleanly ──────────────
            parts = []
            name = item.get("name", "")
            if name:
                parts.append(f"# {name}\n")

            # Ingredients
            ingredients = item.get("recipeIngredient", [])
            if ingredients:
                parts.append("Ingredients:")
                for ing in ingredients:
                    parts.append(f"- {ing}")
                parts.append("")

            # Instructions
            instructions = item.get("recipeInstructions", [])
            if instructions:
                parts.append("Instructions:")
                for i, step in enumerate(instructions, 1):
                    if isinstance(step, dict):
                        text = step.get("text", "")
                    else:
                        text = str(step)
                    if text:
                        parts.append(f"{i}. {text}")
                parts.append("")

            # Useful metadata
            for key, label in [
                ("prepTime", "Prep time"),
                ("cookTime", "Cook time"),
                ("totalTime", "Total time"),
                ("recipeYield", "Yield"),
            ]:
                val = item.get(key)
                if val:
                    # ISO 8601 durations like PT30M → "30M"
                    if isinstance(val, str) and val.startswith("PT"):
                        val = val[2:]
                    parts.append(f"{label}: {val}")

            result = "\n".join(parts)
            if result.strip():
                return result

    return None


def _extract_plain_text(soup: BeautifulSoup) -> str:
    """Fallback: strip junk tags and try to find actual recipe content."""
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "iframe", "form", "button", "svg", "noscript"]):
        tag.decompose()

    # Try to narrow to the main content area (skip sidebars/nav)
    main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
    target = main if main else soup

    text = target.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Find where the recipe actually starts — skip nav/menu junk before it
    recipe_start = 0
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(kw in lower for kw in ["ingredient", "instruction", "direction",
                                       "you will need", "you'll need", "what you need",
                                       "method", "steps", "prep time", "cook time",
                                       "serves", "yield", "total time"]):
            recipe_start = max(0, i - 3)
            break

    if recipe_start > 0:
        lines = lines[recipe_start:]

    return "\n".join(lines)[:MAX_CHARS]


async def fetch_recipe(url: str) -> str:
    """Fetch a recipe URL and return clean recipe text.

    For YouTube URLs: fetches transcript (and optionally title/description via
    YOUTUBE_API_KEY). For other URLs: tries JSON-LD first, then plain-text extraction.
    """
    video_id = _youtube_video_id(url)
    if video_id:
        title, description = await _youtube_description(video_id)
        transcript = None
        dev_api_key = os.getenv("YOUTUBE_TRANSCRIPT_DEV_API_KEY", "").strip()
        if dev_api_key:
            transcript = await _fetch_transcript_via_dev_api(video_id, dev_api_key)
        if transcript is None:
            try:
                transcript = await asyncio.to_thread(_fetch_youtube_transcript_sync, video_id)
            except Exception as e:
                if description:
                    note = "Recipe from video description (no captions available)."
                    return f"{note}\n\n# {title}\n\n{description}" if title else f"{note}\n\n{description}"
                raise ValueError(f"No transcript or description available for this video: {e}") from e

        parts = []
        if title:
            parts.append(f"# Recipe from video: {title}\n")
        if description:
            parts.append("## Description\n")
            parts.append(description)
            parts.append("")
        parts.append("## Transcript\n")
        parts.append(transcript)
        return "\n".join(parts)

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
        for attempt in range(2):
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                break
            except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                if attempt == 0:
                    continue
                raise

    soup = BeautifulSoup(response.text, "html.parser")

    # Prefer structured data — typically ~500-1500 chars of pure recipe info
    structured = _extract_json_ld_recipe(soup)
    if structured:
        return structured

    # Fallback to noisy full-page text
    return _extract_plain_text(soup)


def is_url(value: str) -> bool:
    return value.strip().startswith(("http://", "https://"))
