# kassalapp-mcp

An [MCP](https://modelcontextprotocol.io) server for [Kassalapp](https://kassal.app),
the Norwegian grocery price and product database. It gives an AI assistant read-only
access to product prices, nutrition, ingredients, labels, and cross-store price
comparison for groceries sold in Norwegian stores (KIWI, REMA 1000, Meny, Coop, Spar,
Bunnpris, and more).

Single file, no build step. `uv` reads the dependencies from the script header and runs it.

## Tools

| Tool | What it does |
| --- | --- |
| `health` | Check the API is reachable and your key is accepted. |
| `search_products` | Search groceries by keyword, brand, vendor, store, price range, category. Returns price, nutrition, ingredients, labels. |
| `get_product` | Look up one product by its Kassalapp id. |
| `get_product_by_ean` | Look up a product by EAN barcode, with price comparison across stores. |
| `product_by_url` | Get product info from a store product URL. |
| `compare_by_url` | Given a store product URL, return matching prices from all stores that stock it. |
| `price_history` | Aggregated daily price history across stores for a list of EAN barcodes. |
| `search_stores` | Find physical stores by name, chain, or proximity (lat/lng/radius). |
| `rank_by_nutrition` | Rank products by estimated Nutri-Score, healthiest (A) to worst (E). Same filters as `search_products`. |
| `nutrition_grade` | Estimated Nutri-Score (A-E) for one product by EAN or id, with the full point breakdown. |

## Requirements

- [uv](https://docs.astral.sh/uv/) (handles Python and dependencies).
- A Kassalapp API key. Create one for free (hobby tier, 60 requests/min) at
  <https://kassal.app/api>.

## Setup

### Claude Code (CLI)

```bash
claude mcp add kassalapp --scope user \
  -e KASSALAPP_API_KEY=YOUR_API_KEY \
  -- uv run --quiet /absolute/path/to/kassalapp-mcp/server.py
```

### Claude Desktop / other MCP clients

Add to your client's MCP config (for Claude Desktop, `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "kassalapp": {
      "command": "uv",
      "args": ["run", "--quiet", "/absolute/path/to/kassalapp-mcp/server.py"],
      "env": { "KASSALAPP_API_KEY": "YOUR_API_KEY" }
    }
  }
}
```

Restart the client so it loads the server.

## Usage examples

Once connected, the tools are available to the assistant:

- "Find the cheapest ekstra grov bread at KIWI" -> `search_products(search="grovbrød", store="KIWI", sort="price_asc")`
- "What's the protein content of Kesam?" -> `search_products(search="kesam")`
- "Compare prices for this product across stores" -> `compare_by_url(url="https://meny.no/...")`
- "Price history for these barcodes" -> `price_history(eans=["7039010019811"], days=180)`

## Nutrition score

`rank_by_nutrition` and `nutrition_grade` compute an **estimated Nutri-Score** (A best,
E worst) using the **FSA-NPS 2023 algorithm**, the model behind the European Nutri-Score
label. It scores per 100g from negative components (energy, saturated fat, sugars, salt)
minus positive components (fibre, protein, and fruit/vegetable/legume/nut content), with
separate tables for beverages and for added fats/oils/nuts so food groups are judged
fairly. The threshold tables are transcribed from the
[Open Food Facts](https://github.com/openfoodfacts/openfoodfacts-server) production
implementation of the Santé publique France workbook.

It is an **estimate**, not the official grade, because:

- Kassalapp exposes no fruit/vegetable/nut percentage (that needs composition data), so
  that positive component is inferred from the product category. Whole produce categories
  get a high default so they are not unfairly penalised.
- Ingredients are not parsed, so the 2023 non-nutritive-sweetener penalty is not applied.
- The food type (beverage / fat / cheese / general) is classified from Open Food Facts
  `categories_tags` when the product is in the local OFF cache (reliable), otherwise from
  the Kassalapp category name. A `kind_override` argument forces it. Each result reports
  `classified_by` (`off_tags` or `category_keywords`).
- `rank_by_nutrition` ranks only within the fetched result set (paged up to the requested
  `size`, capped at 300), not the whole catalogue.

The score is most reliable for comparing items **within a category** (e.g. all bread).

### Official grade from Open Food Facts

`nutrition_grade` prefers the **authoritative** Nutri-Score. When the product's barcode is
in [Open Food Facts](https://world.openfoodfacts.org), it returns OFF's official 2023 grade
and real category taxonomy (`source: "openfoodfacts"`), skipping the local estimate and its
category guessing entirely. It falls back to the local FSA-NPS estimate
(`source: "local-estimate"`) only when OFF does not have the product, or when you pass a
`kind_override` / `fvln_override`.

OFF limits reads to 15 requests/min, so results are cached on disk at
`~/.cache/kassalapp-mcp/off.json` (override with `KASSALAPP_CACHE_DIR`). Cached entries are
reused for **7 days**, then re-fetched to stay current. This cache is what lets repeated
lookups bypass the rate limit. `rank_by_nutrition` stays on the local estimate because
grading a 100-item list live would exceed the OFF rate limit.

## Notes

- The API key is read from the `KASSALAPP_API_KEY` environment variable. It is never
  stored in the source. Keep it out of any committed file.
- Hobby tier is rate limited to 60 requests/min and is for non-commercial use. See the
  [Kassalapp API terms](https://kassal.app/api).
- This project is an independent MCP wrapper and is not affiliated with Kassalapp.

## Credits

The [Kassalapp](https://kassal.app) API and service are built by
**Helge Sverre Hessevik Liseth** ([helgesverre.com](https://helgesverre.com)). This MCP
server is just a thin client on top of that work. All the grocery data, price tracking,
and API come from Kassalapp.

## Development

Run the offline self-check (no network, no key needed):

```bash
uv run server.py --selftest
```

## License

MIT. See [LICENSE](LICENSE).
