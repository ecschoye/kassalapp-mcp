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

import httpx
from mcp.server.fastmcp import FastMCP

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
    """Drop None values so optional args don't get sent as empty query params."""
    return {k: v for k, v in params.items() if v is not None}


def _handle(call):
    """Run an httpx call, return parsed JSON, or a clean error dict on HTTP error."""
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


def _get(path: str, params: dict | None = None):
    c = _get_client()
    return _handle(lambda: c.get(path, params=_clean(params or {})))


def _post(path: str, json: dict):
    c = _get_client()
    return _handle(lambda: c.post(path, json=json))


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
    unique: collapse duplicates by EAN (default True).
    exclude_without_ean: drop products with no barcode (default False).
    """
    return _get("/products", {
        "search": search,
        "brand": brand,
        "vendor": vendor,
        "store": store,
        "price_min": price_min,
        "price_max": price_max,
        "category": category,
        "sort": sort,
        "size": size,
        "unique": unique,
        "exclude_without_ean": exclude_without_ean,
    })


@mcp.tool()
def get_product(product_id: int) -> dict:
    """Get one product by its Kassalapp id: price, nutrition, ingredients, allergens."""
    return _get(f"/products/id/{product_id}")


@mcp.tool()
def get_product_by_ean(ean: str) -> dict:
    """Look up a product by EAN barcode. Includes price comparison across stores."""
    return _get(f"/products/ean/{ean}")


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
        "size": size,
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


def _nutrients_100g(product):
    raw = {n.get("code"): n.get("amount") for n in (product.get("nutrition") or [])}
    v = {k: raw.get(code) for k, code in _NCODES.items()}
    kj, kcal = v["energy_kj"], v["energy_kcal"]
    if kj is not None and kcal is not None:
        # Kassalapp sometimes swaps the kJ and kcal fields. kJ is always the
        # larger of the two (1 kcal = 4.184 kJ), so pick by magnitude.
        v["energy_kj"], v["energy_kcal"] = max(kj, kcal), min(kj, kcal)
    elif kj is None and kcal is not None:
        v["energy_kj"] = kcal * 4.184
    if v["salt"] is None and v["sodium"] is not None:
        v["salt"] = v["sodium"] * 2.5  # salt(g) = sodium(g) * 2.5
    missing = [k for k in ("energy_kj", "sat_fat", "sugars", "salt", "protein")
               if v.get(k) is None]
    return v, missing


def _text(product):
    cats = " ".join(c.get("name", "") for c in (product.get("category") or []))
    return (cats + " " + (product.get("name") or "")).lower()


def _food_kind(product, override=None):
    if override:
        return override
    t = _text(product)
    if any(k in t for k in ("brus", "juice", "saft", "leskedrikk", "iste",
                            "energidrikk", "sportsdrikk", "cola", "smoothie",
                            "nektar", "melk", "drikke", "vann", "kaffe")):
        return "beverage"
    if "ost" in t:
        return "cheese"
    if any(k in t for k in ("olje", "margarin", "smør", "matfett", "majones",
                            "nøtter", "mandler", "peanøtt", "kokosfett")):
        return "fat_oil_nut"
    return "general"


def _is_red_meat(product):
    t = _text(product)
    return any(k in t for k in ("storfe", "svin", "lam", "kjøttdeig", "biff",
                                "karbonade", "kjøttkake", "vilt", "hjort",
                                "elg", "reinsdyr", "kalv"))


def _fvln(product, override=None):
    if override is not None:
        return override
    t = _text(product)
    if any(k in t for k in ("frukt", "grønnsak", "grønt", "bær", "salat",
                            "bønner", "linser", "erter", "belgfrukt", "nøtter",
                            "mandler")):
        return PRODUCE_FVLN
    return 0


def _grade(score, kind, is_water=False):
    if kind == "beverage":
        if is_water:
            return "A"
        return "B" if score <= 2 else "C" if score <= 6 else "D" if score <= 9 else "E"
    if kind == "fat_oil_nut":
        return "A" if score <= -6 else "B" if score <= 2 else "C" if score <= 10 else "D" if score <= 18 else "E"
    return "A" if score <= 0 else "B" if score <= 2 else "C" if score <= 10 else "D" if score <= 18 else "E"


def _score(product, kind_override=None, fvln_override=None):
    """Return (result_dict, missing_list). result is None if too little nutrition."""
    v, missing = _nutrients_100g(product)
    if any(v.get(k) is None for k in ("energy_kj", "sat_fat", "sugars", "salt", "protein")):
        return None, missing
    kind = _food_kind(product, kind_override)
    fvln = _fvln(product, fvln_override)
    red = _is_red_meat(product)

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

    is_water = kind == "beverage" and (v["energy_kj"] or 0) < 10 and (v["sugars"] or 0) < 0.5
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
        "missing": missing,
        "note": "estimated Nutri-Score 2023: FVLN from category, ingredients not parsed",
    }, missing


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
    within the fetched set (up to 100 items), not the whole catalog. Products
    without enough nutrition data are skipped and counted.
    """
    data = _get("/products", {"search": search, "store": store, "brand": brand,
                              "category": category, "size": size})
    if isinstance(data, dict) and data.get("error"):
        return data
    items = data.get("data", []) if isinstance(data, dict) else []
    scored, skipped = [], 0
    for p in items:
        res, _m = _score(p, kind_override)
        if res is None:
            skipped += 1
            continue
        v, _ = _nutrients_100g(p)
        scored.append({
            "name": p.get("name"),
            "grade": res["grade"],
            "score": res["score"],
            "kind": res["kind"],
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
        "note": ("Estimated Nutri-Score (FSA-NPS 2023). Ranked within this result "
                 "set only (size up to 100), not the whole catalog. FVLN inferred "
                 "from category; ingredients not parsed."),
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
    if ean:
        data = _get(f"/products/ean/{ean}")
    elif product_id:
        data = _get(f"/products/id/{product_id}")
    else:
        return {"error": "provide ean or product_id"}
    if isinstance(data, dict) and data.get("error"):
        return data
    d = data.get("data", data) if isinstance(data, dict) else {}
    if isinstance(d, dict) and "products" in d:  # /ean comparison shape
        prods = d.get("products") or [{}]
        prod = dict(prods[0])
        prod["nutrition"] = d.get("nutrition")
    else:  # /id shape
        prod = d
    name = prod.get("name")
    res, missing = _score(prod, kind_override, fvln_override)
    if res is None:
        return {"name": name, "error": "not enough nutrition data to score",
                "missing": missing}
    res["name"] = name
    return res


def _selftest() -> None:
    # _clean drops None but keeps falsy-but-valid values.
    assert _clean({"a": 1, "b": None, "c": 0, "d": False}) == {"a": 1, "c": 0, "d": False}
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

    # Plain water -> A
    w, _ = _score(mk({"energi_kj": 0, "mettet_fett": 0, "sukkerarter": 0,
                      "salt": 0, "protein": 0}, ["Vann"], "Kildevann"))
    assert w["grade"] == "A", w

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

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        mcp.run()
