import json
import httpx
from bs4 import BeautifulSoup

MAX_CHARS = 4000


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

    Tries JSON-LD structured data first (fast, small, accurate).
    Falls back to plain-text extraction if no structured data is found.
    """
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
