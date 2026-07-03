# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Open Food Facts lookup: authoritative Nutri-Score grade + category by EAN.

OFF publishes an official 2023 Nutri-Score grade and a proper category taxonomy
per barcode, so we read it instead of guessing food type locally. Read limit is
15 req/min/IP, so results are cached on disk (shared across processes with a file
lock) and a file-backed rate guard keeps every process under the shared limit.
"""
import atexit
import json
import os
import sys
import threading
import time
from contextlib import contextmanager

import httpx

try:
    import fcntl  # POSIX only; used for cross-process cache/rate locking
except ImportError:  # pragma: no cover - Windows fallback (no cross-proc lock)
    fcntl = None

BASE = "https://world.openfoodfacts.org"
# Request the full nutriscore_data (grade + per-component points + is_* flags),
# the versioned grade object, taxonomy tags, and the two complementary signals
# (processing level, ingredient analysis). One call, everything we surface.
FIELDS = ("code,product_name,nutriscore_grade,nutriscore_version,nutriscore,"
          "nutriscore_data,categories_tags,nova_group,ingredients_analysis_tags")
# OFF requires a descriptive User-Agent (AppName/Version (contact)).
USER_AGENT = "kassalapp-mcp/0.2 (+https://github.com/ecschoye/kassalapp-mcp)"
RATE_LIMIT = 15               # requests per window (OFF product-read limit)
RATE_WINDOW = 60.0            # seconds
SCHEMA = 2                    # bump to auto-invalidate cache entries on shape changes
CACHE_TTL = 7 * 24 * 3600     # successful lookups re-fetched after 7 days
MISS_TTL = 24 * 3600          # not-found lookups re-checked daily (may appear in OFF)
STALE_MAX = 30 * 24 * 3600    # never serve cache older than this, even when rate-limited
MAX_ENTRIES = 5000            # prune oldest entries beyond this to bound growth

_client_ = None
_client_lock = threading.Lock()


def _client() -> httpx.Client:
    global _client_
    if _client_ is None:
        with _client_lock:
            if _client_ is None:
                _client_ = httpx.Client(base_url=BASE, headers={"User-Agent": USER_AGENT},
                                        timeout=15.0)
    return _client_


@atexit.register
def _close_client() -> None:
    global _client_
    if _client_ is not None:
        try:
            _client_.close()
        finally:
            _client_ = None


# --- paths ---------------------------------------------------------------

def _dir() -> str:
    return os.environ.get("KASSALAPP_CACHE_DIR") or os.path.expanduser("~/.cache/kassalapp-mcp")


def _cache_path() -> str:
    return os.path.join(_dir(), "off.json")


def _rate_path() -> str:
    return os.path.join(_dir(), "off_rate.json")


def _lock_path() -> str:
    return os.path.join(_dir(), "off.lock")


# --- cross-process lock + json io ---------------------------------------

@contextmanager
def _locked():
    """Hold an exclusive cross-process lock while reading/modifying the cache and
    rate files. Falls back to a no-op lock where fcntl is unavailable."""
    os.makedirs(_dir(), exist_ok=True)
    if fcntl is None:
        yield
        return
    f = open(_lock_path(), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# --- freshness -----------------------------------------------------------

def _age(entry):
    """Seconds since the entry was fetched, or None if the entry is malformed."""
    if not isinstance(entry, dict):
        return None
    ts = entry.get("fetched_at")
    if not isinstance(ts, (int, float)):
        return None
    return time.time() - ts


def _ttl(entry) -> float:
    # Misses (data is None) expire faster than confirmed grades.
    return MISS_TTL if entry.get("data") is None else CACHE_TTL


def _fresh(entry) -> bool:
    if not isinstance(entry, dict) or entry.get("schema") != SCHEMA:
        return False  # missing/old schema -> refetch
    age = _age(entry)
    return age is not None and age < _ttl(entry)


def _stale_ok(entry) -> bool:
    age = _age(entry)
    return age is not None and age < STALE_MAX


def _prune(cache: dict) -> None:
    if len(cache) <= MAX_ENTRIES:
        return
    # keep the newest MAX_ENTRIES by fetched_at
    ordered = sorted(cache.items(),
                     key=lambda kv: (kv[1].get("fetched_at", 0) if isinstance(kv[1], dict) else 0),
                     reverse=True)
    cache.clear()
    cache.update(dict(ordered[:MAX_ENTRIES]))


# --- rate limiter (file-backed sliding window, shared across processes) ---

def _reserve_slot_locked() -> bool:
    """Must be called while holding _locked(). Returns True and records a call if
    the shared window has room, else False (no write on rejection)."""
    now = time.time()
    calls = [t for t in _read_json(_rate_path(), []) if isinstance(t, (int, float))
             and t > now - RATE_WINDOW]
    if len(calls) >= RATE_LIMIT:
        return False
    calls.append(now)
    _write_json(_rate_path(), calls)
    return True


def _release_slot_locked() -> None:
    """Refund the most recently reserved slot (e.g. when the fetch failed), so a
    transient OFF outage does not burn the shared minute budget on failures."""
    calls = _read_json(_rate_path(), [])
    if calls:
        calls.pop()
        _write_json(_rate_path(), calls)


def _graded(data):
    """The OFF-official grade, or None if the cached entry exists but has no grade
    (its tags/flags are still useful via cached_entry, but not as a grade)."""
    return data if isinstance(data, dict) and data.get("grade") else None


# --- normalization -------------------------------------------------------

def _truthy(x) -> bool:
    """OFF returns its is_* flags inconsistently (int 1, int 0, string '1'), and
    bool('0') is True in Python, so parse explicitly rather than via truthiness."""
    return str(x).strip().lower() in ("1", "true", "yes")


def _components(nd: dict):
    comps = nd.get("components")
    if not isinstance(comps, dict):
        return None
    out = {}
    for side in ("negative", "positive"):
        out[side] = [
            {"id": c.get("id"), "value": c.get("value"),
             "points": c.get("points"), "points_max": c.get("points_max")}
            for c in (comps.get(side) or []) if isinstance(c, dict)
        ]
    return out


def _normalize(product: dict, ean: str) -> dict | None:
    """Normalize an OFF product. Returns a rich entry when the product EXISTS in OFF
    (even if it has no valid grade, so its tags/flags are still cached), or None when
    the product is genuinely absent. The entry's `grade` is None for ungraded products."""
    if not isinstance(product, dict):
        return None
    nd = product.get("nutriscore_data") if isinstance(product.get("nutriscore_data"), dict) else {}
    ns = product.get("nutriscore")
    g2023 = ((ns.get("2023") or {}) if isinstance(ns, dict) else {}).get("grade")
    # Prefer the explicit 2023 grade, then the versioned data grade, then top-level.
    grade = g2023 or nd.get("grade") or product.get("nutriscore_grade")
    has_grade = bool(grade) and grade not in ("unknown", "not-applicable", "")
    tags = product.get("categories_tags") or []

    if not (nd or tags or product.get("product_name")):
        return None  # product genuinely not in OFF

    return {
        "found": True,
        "ean": ean,
        "grade": grade.upper() if has_grade else None,
        "version": "2023" if g2023 else product.get("nutriscore_version"),
        "score": nd.get("score"),
        "negative_points": nd.get("negative_points"),
        "positive_points": nd.get("positive_points"),
        "components": _components(nd),
        "flags": {k: _truthy(nd.get(k)) for k in
                  ("is_beverage", "is_water", "is_cheese",
                   "is_fat_oil_nuts_seeds", "is_red_meat_product")},
        "count_proteins": (_truthy(nd.get("count_proteins"))
                           if nd.get("count_proteins") is not None else None),
        "count_proteins_reason": nd.get("count_proteins_reason"),
        "nova_group": product.get("nova_group"),
        "ingredients_analysis_tags": product.get("ingredients_analysis_tags") or [],
        "categories_tags": tags,
        "product_name": product.get("product_name"),
        "source": "openfoodfacts",
        "url": f"{BASE}/product/{ean}",
    }


def _fetch(ean: str) -> dict | None:
    # ean is a path segment; strip to alphanumerics so it cannot alter the URL.
    safe = "".join(ch for ch in ean if ch.isalnum())
    r = _client().get(f"/api/v2/product/{safe}.json", params={"fields": FIELDS})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    try:
        body = r.json()
    except ValueError:  # non-JSON body (CDN error page, maintenance redirect)
        return None
    if body.get("status") == 0 or "product" not in body:
        return None
    return body["product"]


class OffRateLimited(RuntimeError):
    """Raised when the shared OFF rate window is exhausted and no usable cache exists."""


def off_grade(ean, use_cache: bool = True) -> dict | None:
    """Official OFF Nutri-Score + category for an EAN, or None if OFF lacks it.

    Raises OffRateLimited only when the shared 15/min window is exhausted AND there
    is no cache entry (fresh or within STALE_MAX) to serve instead.
    """
    ean = str(ean).strip()
    if not ean:
        return None

    with _locked():
        cache = _read_json(_cache_path(), {})
        entry = cache.get(ean)
        if use_cache and _fresh(entry):
            return _graded(entry.get("data"))
        reserved = _reserve_slot_locked()
        stale = entry if (not reserved and _stale_ok(entry)) else None

    if not reserved:
        if stale is not None:
            return _graded(stale.get("data"))
        raise OffRateLimited("Open Food Facts rate limit reached (15/min). Try again shortly.")

    try:
        data = _fetch(ean)
    except Exception:
        with _locked():
            _release_slot_locked()  # fetch failed, do not burn the slot
        raise
    result = _normalize(data, ean) if data else None

    with _locked():
        cache = _read_json(_cache_path(), {})  # reload so we don't clobber other writers
        cache[ean] = {"fetched_at": time.time(), "schema": SCHEMA, "data": result}
        _prune(cache)
        _write_json(_cache_path(), cache)
    return _graded(result)


def cached_entry(ean) -> dict | None:
    """Return a fresh cached OFF entry (grade + categories_tags) for an EAN, or
    None. Never hits the network, so it is safe to call in bulk (e.g. ranking)."""
    entry = _read_json(_cache_path(), {}).get(str(ean).strip())
    return entry.get("data") if _fresh(entry) else None


def off_grade_bulk(eans) -> dict:
    """Look up many EANs, cache-first, network only within the shared rate budget.

    Returns {ean: {"status": "cache"|"cache_stale"|"fetched"|"skipped_rate_limited",
    "result": ...}}.
    """
    out = {}
    to_fetch = []
    with _locked():
        cache = _read_json(_cache_path(), {})
        for raw in eans:
            ean = str(raw).strip()
            if not ean or ean in out:
                continue
            entry = cache.get(ean)
            if _fresh(entry):
                out[ean] = {"status": "cache", "result": entry.get("data")}
            elif _reserve_slot_locked():
                to_fetch.append(ean)
            elif _stale_ok(entry):
                out[ean] = {"status": "cache_stale", "result": entry.get("data")}
            else:
                out[ean] = {"status": "skipped_rate_limited", "result": None}

    fetched = {}
    for ean in to_fetch:
        try:
            data = _fetch(ean)
        except Exception as e:  # isolate: one failure must not abort the batch
            with _locked():
                _release_slot_locked()
            out[ean] = {"status": "error", "result": None, "error": str(e)}
            continue
        fetched[ean] = _normalize(data, ean) if data else None
        out[ean] = {"status": "fetched", "result": _graded(fetched[ean])}

    if fetched:
        with _locked():
            cache = _read_json(_cache_path(), {})
            for ean, result in fetched.items():
                cache[ean] = {"fetched_at": time.time(), "schema": SCHEMA, "data": result}
            _prune(cache)
            _write_json(_cache_path(), cache)
    return out


def _selftest() -> None:
    # flag parsing: OFF mixes int and string forms; bool() alone would misfire
    assert _truthy(1) and _truthy("1") and _truthy("true")
    assert not _truthy(0) and not _truthy("0") and not _truthy(None) and not _truthy("")

    # normalize: prefers the 2023 grade, parses flags + components
    mixed = {"nutriscore_grade": "d", "nutriscore_version": "2021",
             "nutriscore": {"2023": {"grade": "c"}}, "categories_tags": ["en:breads"],
             "nova_group": 4,
             "nutriscore_data": {"grade": "c", "score": 6, "is_water": "1", "is_beverage": 0,
                                 "count_proteins": 1,
                                 "components": {"negative": [{"id": "salt", "value": 0.8,
                                                              "points": 4, "points_max": 20}],
                                               "positive": [{"id": "fruits_vegetables_legumes",
                                                             "value": 0, "points": 0, "points_max": 5}]}}}
    n = _normalize(mixed, "123")
    assert n["grade"] == "C" and n["version"] == "2023" and n["score"] == 6, n
    assert n["flags"]["is_water"] is True and n["flags"]["is_beverage"] is False, n["flags"]
    assert n["nova_group"] == 4 and n["count_proteins"] is True
    assert n["components"]["negative"][0]["id"] == "salt", n["components"]
    # ungraded product with tags is STILL cached (grade None), so tags/flags survive
    ug = _normalize({"categories_tags": ["en:sodas"], "nutriscore_grade": "unknown"}, "9")
    assert ug is not None and ug["grade"] is None and ug["categories_tags"] == ["en:sodas"], ug
    assert _graded(ug) is None  # not usable as an official grade
    # genuinely absent product -> None
    assert _normalize({}, "1") is None

    # freshness: fresh within TTL, misses expire faster, malformed ts -> not fresh
    assert _fresh({"fetched_at": time.time(), "schema": SCHEMA, "data": {"grade": "A"}})
    assert not _fresh({"fetched_at": time.time() - CACHE_TTL - 1, "schema": SCHEMA, "data": {"grade": "A"}})
    assert _fresh({"fetched_at": time.time() - MISS_TTL + 100, "schema": SCHEMA, "data": None})
    assert not _fresh({"fetched_at": time.time() - MISS_TTL - 1, "schema": SCHEMA, "data": None})
    assert not _fresh({"fetched_at": "oops", "schema": SCHEMA})
    assert not _fresh({"fetched_at": time.time(), "data": {"grade": "A"}})  # no schema -> stale
    assert not _fresh(None)
    assert _stale_ok({"fetched_at": time.time() - STALE_MAX + 100})
    assert not _stale_ok({"fetched_at": time.time() - STALE_MAX - 1})

    # prune keeps the newest MAX_ENTRIES
    big = {str(i): {"fetched_at": i, "data": None} for i in range(MAX_ENTRIES + 10)}
    _prune(big)
    assert len(big) == MAX_ENTRIES and "9" not in big and str(MAX_ENTRIES + 9) in big

    # file-backed rate limiter: 15 allowed, 16th blocked, shared via disk
    os.environ.setdefault("KASSALAPP_CACHE_DIR",
                           os.path.join(os.path.expanduser("~/.cache"), "kassalapp-mcp-selftest"))
    _write_json(_rate_path(), [])
    with _locked():
        assert all(_reserve_slot_locked() for _ in range(RATE_LIMIT))
        assert not _reserve_slot_locked()
    _write_json(_rate_path(), [])  # reset
    print("off selftest ok (offline)")

    if "--live" in sys.argv:
        _write_json(_rate_path(), [])
        r = off_grade("7039010019811", use_cache=False)  # Grandiosa
        assert r and r["grade"] == "C", r
        print("off live ok:", r["grade"], r["version"], r["categories_tags"][:2])


if __name__ == "__main__":
    _selftest()
