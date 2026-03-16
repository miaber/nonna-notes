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

    # If no explicit proxy is configured but system proxy env vars exist,
    # temporarily clear them so the library connects directly to YouTube.
    _saved_env = {}
    if not proxy_config:
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            if var in os.environ:
                _saved_env[var] = os.environ.pop(var)

    try:
        api = YouTubeTranscriptApi(proxy_config=proxy_config) if proxy_config else YouTubeTranscriptApi()
        fetched = api.fetch(video_id)
    finally:
        os.environ.update(_saved_env)
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
                try:
                    err_body = r.text[:500] if r.text else ""
                    print(f"[recipe_fetcher] youtubetranscript.dev {r.status_code}: {err_body}", flush=True)
                except Exception:
                    print(f"[recipe_fetcher] youtubetranscript.dev {r.status_code}", flush=True)
                return None
            data = r.json()
            if data.get("status") != "completed" or not data.get("data"):
                print(f"[recipe_fetcher] youtubetranscript.dev status={data.get('status')} data={bool(data.get('data'))}", flush=True)
                return None
            transcript = data["data"].get("transcript") or {}
            text = (transcript.get("text") or "").strip()
            return text if text else None
    except Exception as e:
        print(f"[recipe_fetcher] youtubetranscript.dev error: {e}", flush=True)
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


def _extract_content_images(soup: BeautifulSoup) -> list[dict]:
    """Extract images from the blog post body with surrounding context.

    Returns list of {"url": str, "alt": str, "context": str} dicts.
    `context` is the text from the nearest preceding <p> sibling, which usually
    describes the action shown in the photo (e.g. "Fold the chips into the
    dough."). This is far more useful for step matching than alt text alone.
    """
    content = soup.find("div", class_="entry-content") or soup.find("article") or soup.find("main")
    if not content:
        return []

    recipe_card = content.find(class_=lambda c: c and any(
        kw in str(c) for kw in ("wprm-recipe-container", "tasty-recipes", "easyrecipe", "recipe-card")
    ))

    def _preceding_text(img_tag) -> str:
        """Walk backwards from the image to find the nearest preceding paragraph."""
        # Walk up to the direct child of the content container so we get past
        # any nesting like <div class="wp-block-image"><figure><img></figure></div>
        anchor = img_tag
        for parent in img_tag.parents:
            if parent == content:
                break
            anchor = parent
        # Walk previous siblings of the anchor looking for a <p> with text
        for sib in anchor.previous_siblings:
            if getattr(sib, "name", None) == "p":
                txt = sib.get_text(" ", strip=True)
                if len(txt) > 15:
                    return txt
            if getattr(sib, "name", None) in ("figure", "img"):
                break
        return ""

    seen = set()
    results = []
    for img in content.find_all("img"):
        if recipe_card and recipe_card in img.parents:
            continue
        src = img.get("data-lazy-src") or img.get("data-src") or img.get("src", "")
        if not src or "data:image" in src or "pixel" in src or len(src) < 20:
            continue
        try:
            if int(img.get("width", 999)) < 100:
                continue
        except (ValueError, TypeError):
            pass
        if src in seen:
            continue
        seen.add(src)
        alt = (img.get("alt") or "").strip()
        ctx = _preceding_text(img)
        results.append({"url": src, "alt": alt, "context": ctx})
    return results


def _extract_recipe_images(soup: BeautifulSoup) -> list[str]:
    """Extract hero/featured images from a recipe page via JSON-LD or Open Graph."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        expanded = []
        for item in items:
            if isinstance(item, dict) and "@graph" in item:
                expanded.extend(item["@graph"])
            else:
                expanded.append(item)
        for item in expanded:
            if not isinstance(item, dict):
                continue
            schema_type = item.get("@type", "")
            types = schema_type if isinstance(schema_type, list) else [schema_type]
            if "Recipe" not in types:
                continue
            img = item.get("image")
            if not img:
                continue
            urls = []
            if isinstance(img, str):
                urls.append(img)
            elif isinstance(img, list):
                for entry in img[:4]:
                    if isinstance(entry, str):
                        urls.append(entry)
                    elif isinstance(entry, dict) and entry.get("url"):
                        urls.append(entry["url"])
            elif isinstance(img, dict) and img.get("url"):
                urls.append(img["url"])
            if urls:
                return urls[:3]

    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        return [og["content"]]
    return []


def _extract_json_ld_recipe(soup: BeautifulSoup) -> tuple[str | None, dict[int, str]]:
    """Try to pull structured recipe data from JSON-LD (schema.org).

    Returns (recipe_text, step_images) where step_images maps 1-based step
    index to an image URL extracted from HowToStep entries.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items = data if isinstance(data, list) else [data]

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
            types = schema_type if isinstance(schema_type, list) else [schema_type]
            if "Recipe" not in types:
                continue

            parts = []
            step_images: dict[int, str] = {}
            name = item.get("name", "")
            if name:
                parts.append(f"# {name}\n")

            ingredients = item.get("recipeIngredient", [])
            if ingredients:
                parts.append("Ingredients:")
                for ing in ingredients:
                    parts.append(f"- {ing}")
                parts.append("")

            instructions = item.get("recipeInstructions", [])
            if instructions:
                parts.append("Instructions:")
                step_idx = 0
                for entry in instructions:
                    if isinstance(entry, dict):
                        entry_type = entry.get("@type", "")
                        # HowToSection contains a list of HowToSteps
                        if entry_type == "HowToSection":
                            for sub in entry.get("itemListElement", []):
                                step_idx += 1
                                text = sub.get("text", "") if isinstance(sub, dict) else str(sub)
                                if text:
                                    parts.append(f"{step_idx}. {text}")
                                img_url = _step_image_url(sub) if isinstance(sub, dict) else None
                                if img_url:
                                    step_images[step_idx] = img_url
                        else:
                            step_idx += 1
                            text = entry.get("text", "")
                            if text:
                                parts.append(f"{step_idx}. {text}")
                            img_url = _step_image_url(entry)
                            if img_url:
                                step_images[step_idx] = img_url
                    else:
                        step_idx += 1
                        text = str(entry)
                        if text:
                            parts.append(f"{step_idx}. {text}")
                parts.append("")

            for key, label in [
                ("prepTime", "Prep time"),
                ("cookTime", "Cook time"),
                ("totalTime", "Total time"),
                ("recipeYield", "Yield"),
            ]:
                val = item.get(key)
                if val:
                    if isinstance(val, str) and val.startswith("PT"):
                        val = val[2:]
                    parts.append(f"{label}: {val}")

            result = "\n".join(parts)
            if result.strip():
                if step_images:
                    print(f"[recipe-agent] found {len(step_images)} step image(s) in JSON-LD", flush=True)
                return result, step_images

    return None, {}


def _step_image_url(step_dict: dict) -> str | None:
    """Extract the first image URL from a HowToStep JSON-LD entry."""
    img = step_dict.get("image")
    if not img:
        return None
    if isinstance(img, str):
        return img
    if isinstance(img, list) and img:
        first = img[0]
        return first if isinstance(first, str) else first.get("url") if isinstance(first, dict) else None
    if isinstance(img, dict):
        return img.get("url")
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


async def fetch_recipe(url: str) -> tuple[str, list[str], dict[int, str], list[dict]]:
    """Fetch a recipe URL and return (clean_text, hero_images, step_images, content_images).

    step_images: dict mapping 1-based step index to image URL (from JSON-LD HowToStep).
    content_images: list of {"url", "alt"} dicts from the blog post body (for fuzzy step matching).
    """
    video_id = _youtube_video_id(url)
    if video_id:
        images = [f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"]
        title, description = await _youtube_description(video_id)
        transcript = None
        dev_api_key = os.getenv("YOUTUBE_TRANSCRIPT_DEV_API_KEY", "").strip()
        if dev_api_key:
            transcript = await _fetch_transcript_via_dev_api(video_id, dev_api_key)
            # On Cloud Run we never try youtube_transcript_api — YouTube returns 403 for datacenter IPs.
            # If dev API failed, go straight to description fallback or raise.
            if transcript is None and description:
                print(f"[recipe_fetcher] youtubetranscript.dev failed, using video description for {video_id}", flush=True)
                note = "Recipe from video description (no captions available)."
                text = f"{note}\n\n# {title}\n\n{description}" if title else f"{note}\n\n{description}"
                return text, images, {}, []
            if transcript is None:
                raise ValueError(
                    "No transcript available for this video. Check YOUTUBE_TRANSCRIPT_DEV_API_KEY and youtubetranscript.dev quota."
                ) from None
        else:
            print("[recipe_fetcher] YOUTUBE_TRANSCRIPT_DEV_API_KEY not set, trying youtube_transcript_api", flush=True)
            try:
                transcript = await asyncio.to_thread(_fetch_youtube_transcript_sync, video_id)
            except Exception as e:
                print(f"[recipe_fetcher] youtube_transcript_api failed: {e}", flush=True)
                if description:
                    print(f"[recipe_fetcher] falling back to video description for {video_id}", flush=True)
                    note = "Recipe from video description (no captions available)."
                    text = f"{note}\n\n# {title}\n\n{description}" if title else f"{note}\n\n{description}"
                    return text, images, {}, []
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
        return "\n".join(parts), images, {}, []

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
    images = _extract_recipe_images(soup)
    content_images = _extract_content_images(soup)

    structured, step_images = _extract_json_ld_recipe(soup)
    if structured:
        return structured, images, step_images, content_images

    return _extract_plain_text(soup), images, {}, content_images


def is_url(value: str) -> bool:
    return value.strip().startswith(("http://", "https://"))
