# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp[cli]", "httpx"]
# ///
"""Kassalapp MCP: read-only tools over the Norwegian grocery price/product API.

https://kassal.app/api/v1 -- product prices, nutrition, labels, store lookup,
cross-store price comparison. Bearer-token auth, key from KASSALAPP_API_KEY.
"""
import os
import sys
import urllib.parse

import httpx
from mcp.server.fastmcp import FastMCP

import off  # Open Food Facts authoritative grade lookup (sibling module)

BASE_URL = "https://kassal.app/api/v1"
mcp = FastMCP("kassalapp")

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        key = os.environ.get("KASSALAPP_API_KEY")
        if not key:
            raise RuntimeError(
                "KASSALAPP_API_KEY is not set. Put it in the MCP config env block."
            )
        _client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=30.0,
        )
    return _client


def _clean(params: dict) -> dict:
    """Drop None values, and encode booleans as 1/0. The Kassalapp API rejects the
    literal `true`/`false` that httpx emits for Python bools ("must be true or
    false") but accepts 1/0, so this normalization keeps boolean filters working."""
    out = {}
    for k, v in params.items():
        if v is None:
            continue
        out[k] = int(v) if isinstance(v, bool) else v
    return out


def _handle(call):
    """Run an httpx call, return parsed JSON, or a clean error dict on any failure.

    Everything is funneled to an error dict (never raised) so an MCP tool always
    returns structured output: HTTP status errors, transport errors, a non-JSON
    200 body, and a missing/invalid API key (RuntimeError from _get_client).
    """
    try:
        resp = call()
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        detail = None
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        return {
            "error": "HTTP error from Kassalapp",
            "status": e.response.status_code,
            "detail": detail,
        }
    except httpx.HTTPError as e:
        return {"error": "request failed", "detail": str(e)}
    except ValueError as e:  # resp.json() on a non-JSON body
        return {"error": "invalid JSON in response", "detail": str(e)}
    except RuntimeError as e:  # e.g. missing KASSALAPP_API_KEY
        return {"error": "configuration error", "detail": str(e)}


def _get(path: str, params: dict | None = None):
    return _handle(lambda: _get_client().get(path, params=_clean(params or {})))


def _post(path: str, json: dict):
    return _handle(lambda: _get_client().post(path, json=json))


def _clamp_size(size: int) -> int:
    """Kassalapp accepts size 1-100; clamp so out-of-range never 422s or over-fetches."""
    try:
        return max(1, min(100, int(size)))
    except (TypeError, ValueError):
        return 20


@mcp.tool()
def health() -> dict:
    """Check that the Kassalapp API is reachable and the API key is accepted."""
    return _get("/health")


@mcp.tool()
def search_products(
    search: str | None = None,
    brand: str | None = None,
    vendor: str | None = None,
    store: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    category: str | None = None,
    sort: str | None = None,
    size: int = 20,
    unique: bool = True,
    exclude_without_ean: bool = False,
) -> dict:
    """Search Norwegian grocery products for price, nutrition, ingredients, and labels.

    search: keyword matched against product name (min 3 chars).
    brand / vendor: filter by brand (e.g. "BAMA") or leverandor (e.g. "orkla foods").
    store: chain code, e.g. "KIWI", "REMA_1000", "MENY_NO", "SPAR_NO", "COOP_EXTRA".
    price_min / price_max: price bounds in NOK.
    category: category name, e.g. "bakeri".
    sort: date_asc, date_desc, name_asc, name_desc, price_asc, price_desc.
    size: results per page, 1-100 (default 20).
    unique: collapse duplicates by EAN (default True). Done client-side, since the
      API rejects its own `unique` query param.
    exclude_without_ean: drop products with no barcode (default False).
    """
    data = _get("/products", {
        "search": search,
        "brand": brand,
        "vendor": vendor,
        "store": store,
        "price_min": price_min,
        "price_max": price_max,
        "category": category,
        "sort": sort,
        "size": _clamp_size(size),
        "exclude_without_ean": exclude_without_ean,
    })
    if unique and isinstance(data, dict) and isinstance(data.get("data"), list):
        data["data"] = _dedupe_by_ean(data["data"])
    return data


def _dedupe_by_ean(items: list) -> list:
    """Collapse duplicate products by EAN, keeping the first. Items without an EAN
    are all kept (we cannot tell them apart)."""
    seen, out = set(), []
    for p in items:
        ean = p.get("ean") if isinstance(p, dict) else None
        if ean and ean in seen:
            continue
        if ean:
            seen.add(ean)
        out.append(p)
    return out


def _safe_seg(value) -> str:
    """URL-encode a path segment so an EAN/id cannot alter the request path."""
    return urllib.parse.quote(str(value), safe="")


@mcp.tool()
def get_product(product_id: int) -> dict:
    """Get one product by its Kassalapp id: price, nutrition, ingredients, allergens."""
    return _get(f"/products/id/{_safe_seg(product_id)}")


@mcp.tool()
def get_product_by_ean(ean: str) -> dict:
    """Look up a product by EAN barcode. Includes price comparison across stores."""
    return _get(f"/products/ean/{_safe_seg(ean)}")


@mcp.tool()
def product_by_url(url: str) -> dict:
    """Get product info from a store product URL (e.g. a meny.no product page)."""
    return _get("/products/find-by-url/single", {"url": url})


@mcp.tool()
def compare_by_url(url: str) -> dict:
    """Given a store product URL, return matching prices from all stores that stock it."""
    return _get("/products/find-by-url/compare", {"url": url})


@mcp.tool()
def price_history(eans: list[str], days: int = 180, aggregation: str | None = None) -> dict:
    """Aggregated daily price history across stores for a list of EAN barcodes.

    eans: list of EAN barcode strings.
    days: how many days of history (default 180).
    aggregation: optional aggregation mode accepted by the API.
    """
    body = _clean({"eans": eans, "days": days, "aggregation": aggregation})
    return _post("/products/prices-bulk", body)


@mcp.tool()
def search_stores(
    search: str | None = None,
    group: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    km: float | None = None,
    size: int = 20,
) -> dict:
    """Find physical grocery stores by name, chain, or proximity.

    search: keyword, e.g. a place or store name.
    group: chain code, e.g. "KIWI", "REMA_1000", "MENY_NO", "BUNNPRIS".
    lat / lng / km: proximity search around a coordinate within km radius.
    size: results per page, 1-100 (default 20).
    """
    return _get("/physical-stores", {
        "search": search,
        "group": group,
        "lat": lat,
        "lng": lng,
        "km": km,
        "size": _clamp_size(size),
    })


# ---------------------------------------------------------------------------
# Estimated Nutri-Score (FSA-NPS 2023 algorithm).
#
# Threshold tables transcribed verbatim from the Open Food Facts production
# implementation (lib/ProductOpener/Nutriscore.pm, points_thresholds_2023),
# which encodes the Sante publique France 2023 workbook. Points = number of
# thresholds the value strictly exceeds (value > t), except the saturated-fat
# ratio which uses value >= t.
#
# ponytail: "estimated" because Kassalapp exposes no FVLN % and no ingredient
# list, so the fruit/veg/legume/nut score is guessed from category and the
# 2023 non-nutritive-sweetener +4 penalty is not applied. Upgrade path: parse
# ingredients for real FVLN and sweetener detection.
# ---------------------------------------------------------------------------

_T = {
    "energy": [335, 670, 1005, 1340, 1675, 2010, 2345, 2680, 3015, 3350],
    "sugars": [3.4, 6.8, 10, 14, 17, 20, 24, 27, 31, 34, 37, 41, 44, 48, 51],
    "sat_fat": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    "salt": [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0,
             2.2, 2.4, 2.6, 2.8, 3.0, 3.2, 3.4, 3.6, 3.8, 4.0],
    "fiber": [3.0, 4.1, 5.2, 6.3, 7.4],
    "protein": [2.4, 4.8, 7.2, 9.6, 12, 14, 17],
    "fvln": [40, 60, 80, 80, 80],
    "energy_bev": [30, 90, 150, 210, 240, 270, 300, 330, 360, 390],
    "sugars_bev": [0.5, 2, 3.5, 5, 6, 7, 8, 9, 10, 11],
    "fvln_bev": [40, 40, 60, 60, 80, 80],
    "protein_bev": [1.2, 1.5, 1.8, 2.1, 2.4, 2.7, 3.0],
    "energy_satfat": [120, 240, 360, 480, 600, 720, 840, 960, 1080, 1200],
    "satfat_ratio": [10, 16, 22, 28, 34, 40, 46, 52, 58, 64],
}

_KJ_PER_G_FAT = 37.0
PRODUCE_FVLN = 100  # whole fruit/veg/legume/nut assumption for the override
_VALID_KINDS = ("general", "beverage", "fat_oil_nut", "cheese")
RANK_MAX = 300  # hard cap on products a single ranking will fetch across pages

_NCODES = {
    "energy_kj": "energi_kj", "energy_kcal": "energi_kcal",
    "sat_fat": "mettet_fett", "sugars": "sukkerarter", "salt": "salt",
    "sodium": "natrium", "fiber": "kostfiber", "protein": "protein",
    "fat": "fett_totalt",
}


def _pts(value, key, ge=False):
    if value is None:
        return 0
    return sum(1 for t in _T[key] if (value >= t if ge else value > t))


def _kcal_floor(v):
    """Lower bound on kcal/100g from known macros (Atwater). None if too little
    data. sugars is only part of carbohydrate, so this is a genuine floor."""
    fat, protein, sugars = v.get("fat"), v.get("protein"), v.get("sugars")
    if fat is None and protein is None:
        return None
    return 9 * (fat or 0) + 4 * (protein or 0) + 4 * (sugars or 0)


def _nutrients_100g(product):
    raw = {n.get("code"): n.get("amount") for n in (product.get("nutrition") or [])}
    v = {k: raw.get(code) for k, code in _NCODES.items()}
    if v["salt"] is None and v["sodium"] is not None:
        v["salt"] = v["sodium"] * 2.5  # salt(g) = sodium(g) * 2.5

    kj, kcal = v["energy_kj"], v["energy_kcal"]
    if kj is not None and kcal is not None:
        # Kassalapp sometimes swaps the two fields. kJ is always the larger
        # (1 kcal = 4.184 kJ), so pick by magnitude.
        v["energy_kj"], v["energy_kcal"] = max(kj, kcal), min(kj, kcal)
    elif kcal is not None:  # only kcal present
        v["energy_kj"], v["energy_kcal"] = kcal * 4.184, kcal
    elif kj is not None:  # only "kj" present; verify it is really kJ, not mislabeled kcal
        floor = _kcal_floor(v)
        if floor and (kj / 4.184) < floor * 0.85:
            # implied kcal is below what the macros alone require -> the value is
            # actually kcal in the kJ slot; convert it up.
            v["energy_kj"], v["energy_kcal"] = kj * 4.184, kj
        else:
            v["energy_kcal"] = kj / 4.184

    missing = [k for k in ("energy_kj", "sat_fat", "sugars", "salt", "protein")
               if v.get(k) is None]
    return v, missing


# Open Food Facts categories_tags are canonical, hierarchical, and expanded to
# include every ancestor, so exact tag membership is a reliable classifier (much
# better than Norwegian substring guessing). These drive food-type / red-meat /
# produce when the product is in the OFF cache.
_TAG_WATER = {"en:waters", "en:mineral-waters", "en:spring-waters",
              "en:natural-mineral-waters", "en:sparkling-waters"}
_TAG_BEVERAGE = {"en:beverages", "en:waters", "en:plant-based-beverages", "en:milks"}
_TAG_CHEESE = {"en:cheeses"}
_TAG_FAT = {"en:fats", "en:vegetable-oils", "en:olive-oils", "en:nuts",
            "en:seeds", "en:margarines", "en:butters"}
_TAG_REDMEAT = {"en:red-meats", "en:beef", "en:pork", "en:lamb-meats", "en:veals",
                "en:game-meats"}
_TAG_PRODUCE = {"en:fruits", "en:vegetables", "en:legumes", "en:nuts",
                "en:fresh-fruits", "en:fresh-vegetables", "en:frozen-vegetables"}

# Keyword fallback (only when a product is NOT in the OFF cache). Matched against
# whole category *words*, never substrings, so "ostekake" no longer reads as
# cheese and "kaffebrød" no longer reads as a beverage.
_KW_WATER = {"vann", "mineralvann", "kildevann"}
_KW_BEVERAGE = {"brus", "juice", "saft", "leskedrikk", "iste", "energidrikk",
                "sportsdrikk", "smoothie", "nektar", "drikke", "kaffe", "te",
                "melk", "mineralvann", "vann", "kildevann"}
_KW_CHEESE = {"ost", "oster"}
_KW_FAT = {"olje", "oljer", "margarin", "smør", "matfett", "majones", "nøtter",
           "mandler", "peanøtter", "kokosfett", "fett"}
_KW_REDMEAT = {"storfe", "svin", "svinekjøtt", "lam", "kjøttdeig", "biff",
               "karbonade", "kjøttkaker", "vilt", "hjort", "elg", "reinsdyr", "kalv"}
_KW_PRODUCE = {"frukt", "grønnsaker", "grønnsak", "grønt", "bær", "salat",
               "bønner", "linser", "erter", "belgfrukter", "nøtter", "mandler"}


def _kind_from_tags(tags):
    if not tags:
        return None
    t = set(tags)
    if t & _TAG_BEVERAGE:
        return "beverage"
    if t & _TAG_CHEESE:
        return "cheese"
    if t & _TAG_FAT:
        return "fat_oil_nut"
    return "general"


def _redmeat_from_tags(tags):
    return None if not tags else bool(set(tags) & _TAG_REDMEAT)


def _fvln_from_tags(tags):
    if not tags:
        return None
    return PRODUCE_FVLN if set(tags) & _TAG_PRODUCE else 0


def _cat_words(product):
    # Word tokens from category names only (curated), NOT the free-text product
    # name. Whole-word matching avoids the substring traps (ostekake, fruktyoghurt).
    text = " ".join(c.get("name", "") for c in (product.get("category") or [])).lower()
    return {w for w in "".join(ch if ch.isalpha() else " " for ch in text).split()}


def _food_kind(product, override=None, tags=None):
    if override:
        return override
    tk = _kind_from_tags(tags)
    if tk is not None:
        return tk
    w = _cat_words(product)
    if w & _KW_BEVERAGE:
        return "beverage"
    if w & _KW_CHEESE:
        return "cheese"
    if w & _KW_FAT:
        return "fat_oil_nut"
    return "general"


def _is_water(product, tags, v):
    """Only genuine water gets Nutri-Score A automatically. Requires a water signal
    (OFF water tag or a water category word) plus near-zero energy and sugar, so
    black coffee / diet drinks do not slip through."""
    watery = (tags and set(tags) & _TAG_WATER) or bool(_cat_words(product) & _KW_WATER)
    return bool(watery) and (v.get("energy_kj") or 0) < 10 and (v.get("sugars") or 0) < 0.5


def _is_red_meat(product, tags=None):
    tr = _redmeat_from_tags(tags)
    if tr is not None:
        return tr
    return bool(_cat_words(product) & _KW_REDMEAT)


def _fvln(product, override=None, tags=None):
    if override is not None:
        return override
    tf = _fvln_from_tags(tags)
    if tf is not None:
        return tf
    return PRODUCE_FVLN if _cat_words(product) & _KW_PRODUCE else 0


def _grade(score, kind, is_water=False):
    if kind == "beverage":
        if is_water:
            return "A"
        return "B" if score <= 2 else "C" if score <= 6 else "D" if score <= 9 else "E"
    if kind == "fat_oil_nut":
        return "A" if score <= -6 else "B" if score <= 2 else "C" if score <= 10 else "D" if score <= 18 else "E"
    return "A" if score <= 0 else "B" if score <= 2 else "C" if score <= 10 else "D" if score <= 18 else "E"


def _score(product, kind_override=None, fvln_override=None, tags=None):
    """Return (result_dict, missing_list). result is None if too little nutrition.

    tags: OFF categories_tags for this product, when known. When present they
    drive classification (reliable); otherwise fall back to category keywords.
    """
    v, missing = _nutrients_100g(product)
    kind = _food_kind(product, kind_override, tags)
    # Added fats/oils are scored on the saturated-fat-to-total-fat ratio, so total
    # fat is required for them, otherwise the ratio silently reads as 0 points.
    required = ("energy_kj", "sat_fat", "sugars", "salt", "protein")
    if kind == "fat_oil_nut":
        required = required + ("fat",)
    missing = [k for k in required if v.get(k) is None]
    if missing:
        return None, missing
    fvln = _fvln(product, fvln_override, tags)
    red = _is_red_meat(product, tags)

    if kind == "beverage":
        neg = {"energy": _pts(v["energy_kj"], "energy_bev"),
               "sugars": _pts(v["sugars"], "sugars_bev"),
               "sat_fat": _pts(v["sat_fat"], "sat_fat"),
               "salt": _pts(v["salt"], "salt")}
    elif kind == "fat_oil_nut":
        efs = v["sat_fat"] * _KJ_PER_G_FAT
        ratio = (100 * v["sat_fat"] / v["fat"]) if v.get("fat") else None
        neg = {"energy": _pts(efs, "energy_satfat"),
               "sat_fat": _pts(ratio, "satfat_ratio", ge=True),
               "sugars": _pts(v["sugars"], "sugars"),
               "salt": _pts(v["salt"], "salt")}
    else:  # general or cheese
        neg = {"energy": _pts(v["energy_kj"], "energy"),
               "sugars": _pts(v["sugars"], "sugars"),
               "sat_fat": _pts(v["sat_fat"], "sat_fat"),
               "salt": _pts(v["salt"], "salt")}
    negative = sum(neg.values())

    if kind == "beverage":
        fvln_pts = _pts(fvln, "fvln_bev")
        protein_pts = _pts(v["protein"], "protein_bev")
    else:
        fvln_pts = _pts(fvln, "fvln")
        protein_pts = _pts(v["protein"], "protein")
    fiber_pts = _pts(v["fiber"], "fiber")
    if red and protein_pts > 2:
        protein_pts = 2

    if kind in ("beverage", "cheese"):
        count_prot = True
    elif kind == "fat_oil_nut":
        count_prot = negative < 7
    else:
        count_prot = negative < 11
    positive = fiber_pts + fvln_pts + (protein_pts if count_prot else 0)

    is_water = kind == "beverage" and _is_water(product, tags, v)
    score = negative - positive
    return {
        "grade": _grade(score, kind, is_water),
        "score": score,
        "kind": kind,
        "negative_points": neg,
        "positive_points": {"fiber": fiber_pts, "fvln": fvln_pts,
                            "protein": protein_pts if count_prot else 0,
                            "protein_counted": count_prot},
        "fvln_assumed": fvln,
        "red_meat_cap": red,
        "classified_by": "off_tags" if tags else "category_keywords",
        "missing": missing,
        "note": "estimated Nutri-Score 2023: FVLN from category, ingredients not parsed",
    }, missing


def _fetch_products_paged(params, want):
    """Fetch up to `want` products across pages (Kassalapp caps a page at 100 and
    reports no total, so we page until a short/empty batch). Returns (items, error)."""
    items, page = [], 1
    while len(items) < want:
        per = min(100, want - len(items))
        data = _get("/products", {**params, "size": per, "page": page})
        if isinstance(data, dict) and data.get("error"):
            return items, data
        batch = data.get("data", []) if isinstance(data, dict) else []
        items.extend(batch)
        if len(batch) < per:  # last page reached
            break
        page += 1
    return items[:want], None


@mcp.tool()
def rank_by_nutrition(
    search: str | None = None,
    store: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    size: int = 50,
    kind_override: str | None = None,
) -> dict:
    """Rank grocery products by estimated Nutri-Score, healthiest (A) to worst (E).

    Uses the FSA-NPS 2023 algorithm on the products matching your filters, then
    sorts them. Same filters as search_products (search, store, brand, category).
    size: how many products to fetch and rank, 1-100 (default 50).
    kind_override: force the food type when the category is wrong or missing, one
      of "general", "beverage", "fat_oil_nut", "cheese".

    Caveats: it is an ESTIMATE (Kassalapp exposes no fruit/veg content or
    ingredient list, so that part is inferred from category). It ranks only
    within the fetched set (size, paged, capped at 300), not the whole catalog.
    Products without enough nutrition data are skipped and counted.
    """
    if kind_override is not None and kind_override not in _VALID_KINDS:
        return {"error": f"kind_override must be one of {_VALID_KINDS}"}
    want = max(1, min(RANK_MAX, int(size) if str(size).lstrip("-").isdigit() else 50))
    items, err = _fetch_products_paged(
        {"search": search, "store": store, "brand": brand, "category": category}, want)
    if err:
        return err
    scored, skipped, off_classified = [], 0, 0
    for p in items:
        # OFF categories_tags from cache (no network) give reliable classification
        ean = p.get("ean")
        off_entry = off.cached_entry(ean) if ean else None
        tags = off_entry.get("categories_tags") if off_entry else None
        res, _m = _score(p, kind_override, tags=tags)
        if res is None:
            skipped += 1
            continue
        if tags:
            off_classified += 1
        v, _ = _nutrients_100g(p)
        scored.append({
            "name": p.get("name"),
            "grade": res["grade"],
            "score": res["score"],
            "kind": res["kind"],
            "classified_by": res["classified_by"],
            "fiber": v.get("fiber"), "protein": v.get("protein"),
            "sugars": v.get("sugars"), "salt": v.get("salt"),
            "sat_fat": v.get("sat_fat"), "energy_kcal": v.get("energy_kcal"),
            "price": p.get("current_price"),
            "stores": [s.get("code") for s in (p.get("store") or []) if isinstance(s, dict)],
            "ean": p.get("ean"),
        })
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    scored.sort(key=lambda r: (order.get(r["grade"], 9), r["score"]))
    return {
        "count": len(scored),
        "skipped_missing_nutrition": skipped,
        "classified_from_off_tags": off_classified,
        "note": ("Estimated Nutri-Score (FSA-NPS 2023). Ranked within the fetched "
                 "set only (paged up to the requested size, capped at 300), not the "
                 "whole catalog. Food type is classified from Open Food Facts "
                 "categories_tags when the product is in the local OFF cache, "
                 "otherwise from Kassalapp category names. Ingredients not parsed, "
                 "so the diet-sweetener penalty is not applied."),
        "ranked": scored,
    }


@mcp.tool()
def nutrition_grade(
    ean: str | None = None,
    product_id: int | None = None,
    kind_override: str | None = None,
    fvln_override: float | None = None,
) -> dict:
    """Estimated Nutri-Score (A-E) for one product, with the full point breakdown.

    Identify the product by ean or product_id. Returns the grade, the raw score,
    and the negative/positive point breakdown so the result is transparent.
    kind_override: force the food type ("general", "beverage", "fat_oil_nut",
      "cheese") if the category is wrong. fvln_override: set the fruit/veg/nut
      percent (0-100) directly instead of inferring it from category.
    """
    if kind_override is not None and kind_override not in _VALID_KINDS:
        return {"error": f"kind_override must be one of {_VALID_KINDS}"}
    if fvln_override is not None:
        if not isinstance(fvln_override, (int, float)) or not 0 <= fvln_override <= 100:
            return {"error": "fvln_override must be a number between 0 and 100"}

    if ean:
        data = _get(f"/products/ean/{_safe_seg(ean)}")
    elif product_id:
        data = _get(f"/products/id/{_safe_seg(product_id)}")
    else:
        return {"error": "provide ean or product_id"}
    if isinstance(data, dict) and data.get("error"):
        return data
    d = data.get("data", data) if isinstance(data, dict) else {}
    if isinstance(d, dict) and "products" in d:  # /ean comparison shape
        prods = d.get("products") or [{}]
        prod = dict(prods[0])
        # Nutrition lives at the comparison root; keep the product's own if the
        # root lacks it, so we never null out data that is actually present.
        prod["nutrition"] = d.get("nutrition") or prod.get("nutrition")
    else:  # /id shape
        prod = d if isinstance(d, dict) else {}
    name = prod.get("name")

    # Prefer the official Open Food Facts grade when the product is in OFF. Skip
    # it when the caller passed an override, which is meant to steer the local
    # estimate. An OFF error (rate limit, outage) does NOT silently vanish: fall
    # back to local but flag it in the source so the downgrade is visible.
    ean_for_off = ean or prod.get("ean")
    off_error = None
    if ean_for_off and kind_override is None and fvln_override is None:
        try:
            off_res = off.off_grade(str(ean_for_off))
        except Exception as e:
            off_res, off_error = None, str(e)
        if off_res:
            return {
                "name": name,
                "ean": ean_for_off,
                "grade": off_res["grade"],
                "version": off_res.get("version"),
                "categories_tags": off_res.get("categories_tags"),
                "url": off_res.get("url"),
                "source": "openfoodfacts",
                "note": "official Open Food Facts Nutri-Score",
            }

    res, missing = _score(prod, kind_override, fvln_override)
    if res is None:
        return {"name": name, "error": "not enough nutrition data to score",
                "missing": missing}
    res["name"] = name
    res["source"] = "local-fallback-after-off-error" if off_error else "local-estimate"
    if off_error:
        res["off_error"] = off_error
    return res


def _selftest() -> None:
    # _clean drops None, keeps falsy numbers, and encodes bools as 1/0 (the API
    # rejects httpx's "true"/"false").
    assert _clean({"a": 1, "b": None, "c": 0, "d": False, "e": True}) == {"a": 1, "c": 0, "d": 0, "e": 1}
    # Bearer header is built from the env key.
    os.environ["KASSALAPP_API_KEY"] = "testkey"
    global _client
    _client = None
    c = _get_client()
    assert c.headers["Authorization"] == "Bearer testkey"
    assert str(c.base_url).rstrip("/") == BASE_URL
    # HTTP errors become a clean dict, not an exception.
    def _raise():
        req = httpx.Request("GET", BASE_URL + "/x")
        resp = httpx.Response(422, json={"message": "bad"}, request=req)
        resp.raise_for_status()
    out = _handle(_raise)
    assert out["status"] == 422 and out["error"].startswith("HTTP error"), out

    # --- Nutri-Score table checks (FSA-NPS 2023) ---
    def mk(nut, cats=None, name=""):
        return {"name": name, "category": [{"name": c} for c in (cats or [])],
                "nutrition": [{"code": k, "amount": v} for k, v in nut.items()]}

    # High-fiber low-salt grovbrød -> A
    g, _ = _score(mk({"energi_kj": 920, "mettet_fett": 0.5, "sukkerarter": 1.1,
                      "salt": 0.6, "kostfiber": 11, "protein": 11},
                     ["Brød og bakevarer"], "Grovbrød"))
    assert g["kind"] == "general" and g["grade"] == "A", g

    # Sugary cola -> E (beverage table)
    c, _ = _score(mk({"energi_kj": 180, "mettet_fett": 0, "sukkerarter": 11,
                      "salt": 0, "protein": 0}, ["Brus og leskedrikk"], "Cola"))
    assert c["kind"] == "beverage" and c["grade"] == "E", c

    # Plain water -> A (water category word + near-zero energy/sugar)
    w, _ = _score(mk({"energi_kj": 0, "mettet_fett": 0, "sukkerarter": 0,
                      "salt": 0, "protein": 0}, ["Vann"], "Kildevann"))
    assert w["grade"] == "A", w

    # Black coffee is a beverage but NOT water -> must not get the automatic A
    bc, _ = _score(mk({"energi_kj": 8, "mettet_fett": 0, "sukkerarter": 0,
                       "salt": 0, "protein": 0.2}, ["Kaffe"], "Filterkaffe"))
    assert bc["kind"] == "beverage" and bc["grade"] != "A", bc

    # Added fat with total fat MISSING -> unscorable (was silently under-penalized)
    of, miss = _score(mk({"energi_kj": 3400, "mettet_fett": 50, "sukkerarter": 0,
                          "salt": 0, "protein": 0}, ["Olje og fett"], "Rapsolje"))
    assert of is None and "fat" in miss, (of, miss)

    # kJ/kcal swap on a single-field product: 250 in the kJ slot with heavy macros
    # is really kcal (~1046 kJ), detected via the macro floor.
    vsw, _ = _nutrients_100g(mk({"energi_kj": 250, "fett_totalt": 20, "mettet_fett": 5,
                                 "sukkerarter": 0, "salt": 0.5, "protein": 5}))
    assert 1000 < vsw["energy_kj"] < 1100, vsw["energy_kj"]

    # "ostekake" (cheesecake) must NOT classify as cheese via the word fallback
    ok = _food_kind(mk({}, ["Ostekake og dessert"], "Ostekake"))
    assert ok == "general", ok

    # dedupe collapses repeated EANs, keeps ean-less items
    dd = _dedupe_by_ean([{"ean": "1"}, {"ean": "1"}, {"ean": "2"}, {"name": "x"}])
    assert len(dd) == 3, dd
    # size clamp
    assert _clamp_size(100000) == 100 and _clamp_size(0) == 1 and _clamp_size("bad") == 20

    # Spinach with produce FVLN override -> A
    s, _ = _score(mk({"energi_kj": 100, "mettet_fett": 0, "sukkerarter": 0.4,
                      "salt": 0.1, "kostfiber": 2, "protein": 3},
                     ["Frukt og grønt"], "Spinat"))
    assert s["kind"] == "general" and s["fvln_assumed"] == PRODUCE_FVLN and s["grade"] == "A", s

    # Olive oil -> C via the fats/oils table, not E
    o, _ = _score(mk({"energi_kj": 3400, "mettet_fett": 14, "fett_totalt": 92,
                      "sukkerarter": 0, "salt": 0, "protein": 0},
                     ["Olje og fett"], "Olivenolje"))
    assert o["kind"] == "fat_oil_nut" and o["grade"] == "C", o

    # --- classification from OFF categories_tags ---
    assert _kind_from_tags(["en:beverages", "en:sodas"]) == "beverage"
    assert _kind_from_tags(["en:cheeses", "en:dairies"]) == "cheese"
    assert _kind_from_tags(["en:olive-oils", "en:fats"]) == "fat_oil_nut"
    assert _kind_from_tags(["en:pizzas"]) == "general"
    assert _kind_from_tags(None) is None
    assert _redmeat_from_tags(["en:red-meats", "en:beef"]) is True
    assert _redmeat_from_tags(["en:chicken"]) is False
    assert _redmeat_from_tags(None) is None
    assert _fvln_from_tags(["en:vegetables"]) == PRODUCE_FVLN
    assert _fvln_from_tags(["en:pizzas"]) == 0

    # OFF tag beats a misleading name: milk chocolate is NOT a beverage
    mc, _ = _score(mk({"energi_kj": 2200, "mettet_fett": 18, "sukkerarter": 55,
                       "salt": 0.2, "protein": 7}, ["Sjokolade"], "Melkesjokolade"),
                   tags=["en:chocolates", "en:milk-chocolates"])
    assert mc["kind"] == "general" and mc["classified_by"] == "off_tags", mc

    # Keyword fallback now reads category names only, so the name no longer
    # misclassifies: a fruit yoghurt is not treated as 100% produce.
    fy, _ = _score(mk({"energi_kj": 380, "mettet_fett": 2, "sukkerarter": 12,
                       "salt": 0.1, "kostfiber": 0, "protein": 5},
                      ["Yoghurt og syrnet"], "Fruktyoghurt"))
    assert fy["fvln_assumed"] == 0, fy

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        mcp.run()
