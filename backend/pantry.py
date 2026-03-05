import json
import os

PANTRY_FILE = os.path.join(os.path.dirname(__file__), "pantry.json")


def load() -> list[dict]:
    """Return the pantry as a list of {name, status} dicts."""
    if not os.path.exists(PANTRY_FILE):
        return []
    with open(PANTRY_FILE) as f:
        return json.load(f)


def apply_and_save(updates: list[dict], allow_add: bool = False) -> list[dict]:
    """Merge updates into the persisted pantry and return the new full list.

    Each update is {name: str, status: "have" | "out"}.
    Existing items are always updated in-place.
    New items are only appended when allow_add=True (manual UI adds).
    """
    pantry = load()
    index = {item["name"].lower(): i for i, item in enumerate(pantry)}
    for update in updates:
        key = update["name"].lower()
        if key in index:
            pantry[index[key]] = {"name": update["name"], "status": update["status"]}
        elif allow_add:
            pantry.append({"name": update["name"], "status": update["status"]})
    with open(PANTRY_FILE, "w") as f:
        json.dump(pantry, f, indent=2)
    return pantry


def to_prompt_text(pantry: list[dict]) -> str:
    if not pantry:
        return ""
    have = [p["name"] for p in pantry if p["status"] in ("have", "low")]
    out  = [p["name"] for p in pantry if p["status"] == "out"]
    parts = []
    if have:
        parts.append("Have: " + ", ".join(have))
    if out:
        parts.append("Don't have: " + ", ".join(out))
    return "\n".join(parts)
