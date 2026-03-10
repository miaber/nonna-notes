"""
Recipe parse cache: Firebase Storage when FIREBASE_STORAGE_BUCKET is set, else in-memory.

Blob path: cache/{safe_key}.json with {"recipe": dict, "source": str, "cached_at": float}.
Safe key: yt:VIDEO_ID -> yt_VIDEO_ID; other keys hashed to 32-char hex.
"""

import hashlib
import json
import os
import time

FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET", "").strip()
CACHE_TTL_SEC = int(os.getenv("RECIPE_AGENT_CACHE_TTL_SEC", str(24 * 3600)))

# In-memory fallback when Firebase not configured (lower default to limit memory in local dev)
_MEMORY_CACHE: dict[str, dict] = {}
_MEMORY_CACHE_ORDER: list[str] = []
_MEMORY_CACHE_MAX = int(os.getenv("RECIPE_AGENT_CACHE_MAX", "50"))

_firebase_ready = False


def _safe_cache_key(key: str) -> str:
    """Storage-safe key: yt:xxx -> yt_xxx; long keys hashed to 32 hex chars."""
    if key.startswith("yt:") and len(key) <= 20:
        return key.replace(":", "_")
    if len(key) <= 100 and ":" not in key and "/" not in key:
        return key
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _ensure_firebase():
    global _firebase_ready
    if _firebase_ready:
        return
    import firebase_admin
    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"storageBucket": FIREBASE_STORAGE_BUCKET})
    _firebase_ready = True


def _get_blob(safe_key: str):
    _ensure_firebase()
    from firebase_admin import storage as fb_storage
    return fb_storage.bucket().blob(f"cache/{safe_key}.json")


def get(key: str) -> dict | None:
    """Return cached entry {recipe, source, cached_at} or None if miss/expired."""
    now = time.time()
    safe = _safe_cache_key(key)

    if FIREBASE_STORAGE_BUCKET:
        try:
            data = _get_blob(safe).download_as_text()
            entry = json.loads(data)
            if now - entry.get("cached_at", 0) >= CACHE_TTL_SEC:
                return None
            return entry
        except Exception as e:
            if "404" not in str(e) and "NotFound" not in str(e):
                print(f"[recipe-agent] cache get error: {e}", flush=True)
            return None

    if safe in _MEMORY_CACHE:
        entry = _MEMORY_CACHE[safe]
        if now - entry.get("cached_at", 0) < CACHE_TTL_SEC:
            return entry
        del _MEMORY_CACHE[safe]
        if safe in _MEMORY_CACHE_ORDER:
            _MEMORY_CACHE_ORDER.remove(safe)
    return None


def set(key: str, recipe: dict, source: str) -> None:
    """Store parsed recipe in cache."""
    safe = _safe_cache_key(key)
    now = time.time()
    entry = {"recipe": recipe, "source": source, "cached_at": now}

    if FIREBASE_STORAGE_BUCKET:
        try:
            _get_blob(safe).upload_from_string(
                json.dumps(entry),
                content_type="application/json",
            )
        except Exception as e:
            print(f"[recipe-agent] cache set error: {e}", flush=True)
        return

    while len(_MEMORY_CACHE) >= _MEMORY_CACHE_MAX and _MEMORY_CACHE_ORDER:
        old = _MEMORY_CACHE_ORDER.pop(0)
        _MEMORY_CACHE.pop(old, None)
    _MEMORY_CACHE[safe] = entry
    _MEMORY_CACHE_ORDER.append(safe)


def list_keys() -> list[str]:
    """Return cache key prefixes (safe_key) for debugging. Firebase: list blobs; memory: in-memory keys."""
    if FIREBASE_STORAGE_BUCKET:
        try:
            _ensure_firebase()
            from firebase_admin import storage as fb_storage
            blobs = fb_storage.bucket().list_blobs(prefix="cache/", max_results=500)
            return [b.name.replace("cache/", "").replace(".json", "") for b in blobs]
        except Exception as e:
            print(f"[recipe-agent] cache list error: {e}", flush=True)
            return []
    return list(_MEMORY_CACHE.keys())
