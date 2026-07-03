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
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        mcp.run()
