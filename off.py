# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Open Food Facts lookup: authoritative Nutri-Score grade + category by EAN.

OFF publishes an official 2023 Nutri-Score grade and a proper category taxonomy
per barcode, so we read it instead of guessing food type locally. Read limit is
15 req/min/IP, so results are cached on disk and a rate guard protects bulk use.
"""
import json
import os
import sys
import time

import httpx

BASE = "https://world.openfoodfacts.org"
FIELDS = "code,product_name,nutriscore_grade,nutriscore_version,nutriscore,categories_tags"
# OFF requires a descriptive User-Agent (AppName/Version (contact)).
USER_AGENT = "kassalapp-mcp/0.2 (+https://github.com/ecschoye/kassalapp-mcp)"
RATE_LIMIT = 15          # requests per window
RATE_WINDOW = 60.0       # seconds
CACHE_TTL = 7 * 24 * 3600  # entries older than 7 days are re-fetched

_client_ = None
_cache = None
_calls: list[float] = []


def _client() -> httpx.Client:
    global _client_
    if _client_ is None:
        _client_ = httpx.Client(base_url=BASE, headers={"User-Agent": USER_AGENT},
                                timeout=15.0)
    return _client_


def _cache_path() -> str:
    base = os.environ.get("KASSALAPP_CACHE_DIR") or os.path.expanduser("~/.cache/kassalapp-mcp")
    return os.path.join(base, "off.json")


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_cache_path(), encoding="utf-8") as f:
                _cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save_cache(cache: dict) -> None:
    p = _cache_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, p)


def _allow_request() -> bool:
    """Sliding-window limiter: at most RATE_LIMIT calls per RATE_WINDOW seconds."""
    now = time.monotonic()
    cutoff = now - RATE_WINDOW
    while _calls and _calls[0] < cutoff:
        _calls.pop(0)
    if len(_calls) >= RATE_LIMIT:
        return False
    _calls.append(now)
    return True


def _fresh(entry) -> bool:
    """True if a cache entry exists and is younger than CACHE_TTL."""
    return isinstance(entry, dict) and (time.time() - entry.get("fetched_at", 0)) < CACHE_TTL


def _normalize(product: dict, ean: str) -> dict | None:
    grade = product.get("nutriscore_grade")
    ns = product.get("nutriscore") or {}
    g2023 = ((ns.get("2023") or {}) if isinstance(ns, dict) else {}).get("grade")
    grade = grade or g2023
    if not grade or grade in ("unknown", "not-applicable", ""):
        return None
    return {
        "found": True,
        "ean": ean,
        "grade": grade.upper(),
        "version": product.get("nutriscore_version"),
        "grade_2023": g2023.upper() if g2023 else None,
        "categories_tags": product.get("categories_tags") or [],
        "product_name": product.get("product_name"),
        "source": "openfoodfacts",
        "url": f"{BASE}/product/{ean}",
    }


def _fetch(ean: str) -> dict | None:
    r = _client().get(f"/api/v2/product/{ean}.json", params={"fields": FIELDS})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    body = r.json()
    if body.get("status") == 0 or "product" not in body:
        return None
    return body["product"]


def off_grade(ean, use_cache: bool = True) -> dict | None:
    """Official OFF Nutri-Score + category for an EAN, or None if OFF lacks it."""
    ean = str(ean).strip()
    if not ean:
        return None
    cache = _load_cache()
    entry = cache.get(ean)
    if use_cache and _fresh(entry):
        return entry.get("data")
    if not _allow_request():
        # rate limited: serve a stale cached answer if we have one, better than failing
        if entry is not None:
            return entry.get("data")
        raise RuntimeError("Open Food Facts rate limit reached (15/min). Try again shortly.")
    data = _fetch(ean)
    result = _normalize(data, ean) if data else None
    cache[ean] = {"fetched_at": time.time(), "data": result}
    _save_cache(cache)
    return result


def off_grade_bulk(eans) -> dict:
    """Look up many EANs, cache-first, network only within the rate budget.

    Returns {ean: {"status": "cache"|"fetched"|"skipped_rate_limited", "result": ...}}.
    """
    out = {}
    cache = _load_cache()
    dirty = False
    for raw in eans:
        ean = str(raw).strip()
        if not ean:
            continue
        entry = cache.get(ean)
        if _fresh(entry):
            out[ean] = {"status": "cache", "result": entry.get("data")}
            continue
        if not _allow_request():
            if entry is not None:  # stale but usable when we cannot refresh now
                out[ean] = {"status": "cache_stale", "result": entry.get("data")}
            else:
                out[ean] = {"status": "skipped_rate_limited", "result": None}
            continue
        data = _fetch(ean)
        result = _normalize(data, ean) if data else None
        cache[ean] = {"fetched_at": time.time(), "data": result}
        dirty = True
        out[ean] = {"status": "fetched", "result": result}
    if dirty:
        _save_cache(cache)
    return out


def _selftest() -> None:
    # normalize a well-formed OFF product
    sample = {"nutriscore_grade": "c", "nutriscore_version": "2023",
              "nutriscore": {"2023": {"grade": "c"}},
              "categories_tags": ["en:breads"], "product_name": "Test"}
    n = _normalize(sample, "123")
    assert n and n["grade"] == "C" and n["version"] == "2023" and n["grade_2023"] == "C", n
    assert n["categories_tags"] == ["en:breads"]
    # falls back to the 2023 path when the top-level grade is absent
    assert _normalize({"nutriscore": {"2023": {"grade": "a"}}}, "1")["grade"] == "A"
    # unknown / missing grade -> None
    assert _normalize({"nutriscore_grade": "unknown"}, "1") is None
    assert _normalize({}, "1") is None
    # cache freshness: fresh within TTL, stale past it
    assert _fresh({"fetched_at": time.time()})
    assert not _fresh({"fetched_at": time.time() - CACHE_TTL - 1})
    assert not _fresh(None)
    # rate limiter: 15 allowed, 16th blocked
    global _calls
    _calls = []
    assert all(_allow_request() for _ in range(RATE_LIMIT))
    assert not _allow_request()
    print("off selftest ok (offline)")

    if "--live" in sys.argv:
        _calls = []
        r = off_grade("7039010019811", use_cache=False)  # Grandiosa
        assert r and r["grade"] == "C" and r["version"] == "2023", r
        print("off live ok:", r["grade"], r["version"], r["categories_tags"][:2])


if __name__ == "__main__":
    _selftest()
