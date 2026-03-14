"""
Storage abstraction: Firebase Storage in production, local JSON files in dev.

Each recipe is stored as its own document. Drafts and saved recipes are the same
thing: a recipe doc with draft=true (in progress) or draft=false and saved_at (finalized).
There is no "single current draft" — you can have multiple drafts, listed like any recipe.

  Firebase: users/{user_id}/recipes/{recipe_id}.json
  Local:     recipes/{user_id}/{recipe_id}.json
"""

import json
import os
import time

_DIR = os.path.dirname(__file__)
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET", "").strip()

_firebase_ready = False


def _ensure_firebase():
    global _firebase_ready
    if _firebase_ready:
        return
    import firebase_admin
    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"storageBucket": FIREBASE_STORAGE_BUCKET})
    _firebase_ready = True


def _recipe_blob(user_id: str, recipe_id: str):
    _ensure_firebase()
    from firebase_admin import storage as fb_storage
    bucket = fb_storage.bucket()
    return bucket.blob(f"users/{user_id}/recipes/{recipe_id}.json")


def _local_recipes_dir(user_id: str) -> str:
    path = os.path.join(_DIR, "recipes", user_id)
    os.makedirs(path, exist_ok=True)
    return path


def _local_recipe_path(user_id: str, recipe_id: str) -> str:
    return os.path.join(_local_recipes_dir(user_id), f"{recipe_id}.json")


def _ensure_started_at(entry: dict, recipe_id: str) -> None:
    """So frontend 'Continue' can use started_at; drafts often use id as started_at."""
    if entry.get("draft") and "started_at" not in entry:
        try:
            entry["started_at"] = float(recipe_id)
        except (TypeError, ValueError):
            pass


# ── Single recipe document ─────────────────────────────────────────────────────

def _save_recipe_doc(user_id: str, recipe_id: str, entry: dict) -> None:
    """Write one recipe document. Entry must include id, recipe, photos, draft/saved_at etc."""
    if "photos" not in entry:
        entry["photos"] = []
    if FIREBASE_STORAGE_BUCKET:
        _recipe_blob(user_id, recipe_id).upload_from_string(
            json.dumps(entry),
            content_type="application/json",
        )
        return
    path = _local_recipe_path(user_id, recipe_id)
    with open(path, "w") as f:
        json.dump(entry, f, indent=2)


def _load_recipe_doc(user_id: str, recipe_id: str) -> dict | None:
    """Load one recipe document by id. Returns None if not found."""
    if FIREBASE_STORAGE_BUCKET:
        try:
            data = _recipe_blob(user_id, recipe_id).download_as_text()
            entry = json.loads(data)
            if "photos" not in entry:
                entry["photos"] = []
            entry["id"] = recipe_id
            _ensure_started_at(entry, recipe_id)
            return entry
        except Exception as e:
            if "404" in str(e) or "NotFound" in str(e):
                return None
            raise
    try:
        with open(_local_recipe_path(user_id, recipe_id)) as f:
            entry = json.load(f)
        if "photos" not in entry:
            entry["photos"] = []
        entry["id"] = recipe_id
        _ensure_started_at(entry, recipe_id)
        return entry
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[storage] load_recipe_doc error: {e}", flush=True)
        return None


def _delete_recipe_doc(user_id: str, recipe_id: str) -> None:
    """Delete one recipe document."""
    if FIREBASE_STORAGE_BUCKET:
        try:
            _recipe_blob(user_id, recipe_id).delete()
        except Exception as e:
            if "404" not in str(e) and "NotFound" not in str(e):
                print(f"[storage] delete_recipe_doc error: {e}", flush=True)
        return
    try:
        os.remove(_local_recipe_path(user_id, recipe_id))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[storage] delete_recipe_doc error: {e}", flush=True)


def _list_recipe_ids(user_id: str) -> list[str]:
    """List all recipe document ids for this user (draft and saved)."""
    if FIREBASE_STORAGE_BUCKET:
        from firebase_admin import storage as fb_storage
        bucket = fb_storage.bucket()
        prefix = f"users/{user_id}/recipes/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        ids = []
        for b in blobs:
            name = b.name
            if name.endswith(".json"):
                ids.append(os.path.basename(name).replace(".json", ""))
        return ids
    try:
        dir_path = _local_recipes_dir(user_id)
        ids = []
        for f in os.listdir(dir_path):
            if f.endswith(".json"):
                ids.append(f.replace(".json", ""))
        return ids
    except FileNotFoundError:
        return []


# ── Migration from old single-file layout ───────────────────────────────────────

def _old_monolith_path(user_id: str) -> str | None:
    """Path to old saved_recipes file (local only). None for Firebase."""
    if FIREBASE_STORAGE_BUCKET:
        return None
    if user_id == "default":
        return os.path.join(_DIR, "saved_recipes.json")
    return os.path.join(_DIR, f"saved_recipes_{user_id}.json")


def _old_draft_path(user_id: str) -> str | None:
    """Path to old current_draft file (local only)."""
    if FIREBASE_STORAGE_BUCKET:
        return None
    if user_id == "default":
        return os.path.join(_DIR, "current_draft.json")
    return os.path.join(_DIR, f"current_draft_{user_id}.json")


def _migrate_from_monolith_if_needed(user_id: str) -> None:
    """If new layout is empty and old monolith/draft files exist, migrate once."""
    ids = _list_recipe_ids(user_id)
    if ids:
        return
    if FIREBASE_STORAGE_BUCKET:
        _ensure_firebase()
        try:
            from firebase_admin import storage as fb_storage
            bucket = fb_storage.bucket()
            old_blob = bucket.blob(f"users/{user_id}/saved_recipes.ndjson")
            data = old_blob.download_as_text()
        except Exception:
            return
        lines = [l.strip() for l in data.splitlines() if l.strip()]
        if not lines:
            return
    else:
        old_path = _old_monolith_path(user_id)
        if not old_path or not os.path.isfile(old_path):
            draft_path = _old_draft_path(user_id)
            if draft_path and os.path.isfile(draft_path):
                try:
                    with open(draft_path) as f:
                        entry = json.load(f)
                    if entry.get("draft") and entry.get("recipe"):
                        entry["id"] = str(entry.get("started_at", time.time()))
                        entry["draft"] = True
                        if "photos" not in entry:
                            entry["photos"] = []
                        _save_recipe_doc(user_id, entry["id"], entry)
                        os.remove(draft_path)
                except Exception as e:
                    print(f"[storage] migrate draft error: {e}", flush=True)
            return
        with open(old_path) as f:
            content = f.read()
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if not lines:
            return
    for i, line in enumerate(lines):
        try:
            entry = json.loads(line)
            if "photos" not in entry:
                entry["photos"] = []
            rid = entry.get("id") or str(time.time()) + f"_{i}"
            entry["id"] = rid
            _save_recipe_doc(user_id, rid, entry)
        except Exception as e:
            print(f"[storage] migrate entry error: {e}", flush=True)
    if FIREBASE_STORAGE_BUCKET:
        try:
            old_blob.delete()
        except Exception:
            pass
    else:
        try:
            os.remove(old_path)
        except Exception:
            pass
    draft_path = _old_draft_path(user_id)
    if draft_path and os.path.isfile(draft_path):
        try:
            with open(draft_path) as f:
                entry = json.load(f)
            if entry.get("draft") and entry.get("recipe"):
                entry["id"] = str(entry.get("started_at", time.time()))
                entry["draft"] = True
                if "photos" not in entry:
                    entry["photos"] = []
                _save_recipe_doc(user_id, entry["id"], entry)
            os.remove(draft_path)
        except Exception as e:
            print(f"[storage] migrate draft error: {e}", flush=True)
    print(f"[storage] migrated {len(lines)} entries to per-recipe docs (user={user_id})", flush=True)


# ── Public API (list = saved only; single-doc save/load/delete) ────────────────

def load_entries(user_id: str = "default") -> list[dict]:
    """Return all recipe entries (drafts and saved), most recent first. Each entry has 'id', 'draft', etc."""
    _migrate_from_monolith_if_needed(user_id)
    ids = _list_recipe_ids(user_id)
    entries = []
    for rid in ids:
        entry = _load_recipe_doc(user_id, rid)
        if entry is None:
            continue
        entries.append(entry)
    def _sort_key(e):
        return e.get("saved_at") or e.get("updated_at") or ""
    entries.sort(key=_sort_key, reverse=True)
    return entries


def save_entry(user_id: str, entry: dict) -> str:
    """Save one recipe as its own document. Generates id if missing. Returns the recipe id."""
    recipe_id = entry.get("id") or str(time.time())
    entry["id"] = recipe_id
    if "photos" not in entry:
        entry["photos"] = []
    _save_recipe_doc(user_id, recipe_id, entry)
    if FIREBASE_STORAGE_BUCKET:
        print(f"[storage] saved 1 recipe id={recipe_id} (user={user_id})", flush=True)
    return recipe_id


def load_entry(user_id: str, recipe_id: str) -> dict | None:
    """Load one recipe document by id."""
    return _load_recipe_doc(user_id, recipe_id)


def delete_entry(user_id: str, recipe_id: str) -> None:
    """Delete one recipe document."""
    _delete_recipe_doc(user_id, recipe_id)


def get_storage_mode() -> str:
    return "firebase" if FIREBASE_STORAGE_BUCKET else "local"
