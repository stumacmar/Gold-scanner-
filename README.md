# eBay Vintage Gold Signet Ring Scanner

A single-file command-line tool that scans **eBay UK auctions** for undervalued
vintage gold signet rings by comparing the live current bid against the ring's
gold **melt value** (carat content × weight × live spot price).

## How it works

1. Queries the eBay **Browse API** (`buy/browse/v1/item_summary/search`) for UK
   auctions matching a search term (default `9ct gold signet ring`).
2. Filters to `AUCTION` + `USED` + `EBAY_GB` under a configurable max price.
3. Parses the gold **weight** (grams) and **carat** from the title and
   description with regex (handles `9.4g`, `9.4 g`, `9.4 grams`, `9.4gm`, …).
   Items with no parseable weight are flagged **"weight unknown"**, not dropped.
4. Skips plated / rolled / gold-tone listings.
5. Pulls the **live gold spot price** (metals.dev or goldapi.io, or a hardcoded
   fallback constant) and computes each ring's melt value.
6. Flags auctions where `current_bid < melt × threshold` (default `1.3`) as
   **VALUE** candidates, sorted best-value-first (highest melt-to-bid ratio).
7. Prints a console table and writes the full result set to CSV.

## Get eBay API credentials (free, ~5 min)

1. Sign in at <https://developer.ebay.com>.
2. Open **Developer Account → Application Keys**.
3. Create an application keyset — use the **Production** keyset (not Sandbox) so
   you get real live listings.
4. Copy the **App ID (Client ID)** and **Cert ID (Client Secret)**.

The Browse API uses an OAuth2 **application** token (client-credentials), *not*
a user token. The script fetches and caches that token automatically.

Optionally grab a free gold-price key from <https://metals.dev> or
<https://goldapi.io>. Without one, the script uses the editable
`SPOT_PRICE_GBP_PER_OZ` fallback constant.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit .env with your keys
```

## Run

```bash
python gold_ring_scanner.py
python gold_ring_scanner.py --query "18ct gold signet ring" --max-price 400
python gold_ring_scanner.py --threshold 1.0 --limit 100
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--query` | `9ct gold signet ring` | eBay search term |
| `--max-price` | `250` | Max current price (GBP) to consider |
| `--threshold` | `1.3` | Flag if `bid < melt × threshold` |
| `--limit` | `200` | Max listings to fetch (politeness cap) |
| `--csv` | `gold_ring_results.csv` | Output CSV path |

All other tunables live in the clearly-commented **CONFIG** block at the top of
`gold_ring_scanner.py`.

## Web dashboard (GitHub Pages)

A visual dashboard (`index.html`) shows the latest scan as a dark, mobile-first
list of cards — value candidates highlighted, with carat/weight/bid/melt/ratio,
time left, and a tap-through to each eBay listing. It reads `data/results.json`,
which is refreshed automatically by a scheduled GitHub Action.

**One-time setup:**

1. **Add your eBay keys as repository secrets** — *Settings → Secrets and
   variables → Actions → New repository secret*:
   - `EBAY_CLIENT_ID` — your App ID (Client ID)
   - `EBAY_CLIENT_SECRET` — your Cert ID (Client Secret)

   (The gold price uses a keyless live source, so no price-API secret is needed.)
2. **Enable GitHub Pages** — *Settings → Pages → Build and deployment →
   Source: Deploy from a branch*, pick the branch and `/ (root)` folder.
3. The dashboard is then live at `https://<you>.github.io/<repo>/`.

**Refreshing the data:**

- The `Update gold ring scan` workflow runs daily (07:00 UTC) and commits fresh
  `data/results.json`. Scheduled runs only fire from the **default branch
  (main)** — merge this branch into main to activate the schedule.
- You can also run it any time from the **Actions** tab ("Run workflow").

## Important caveats

- **Weights are seller-claimed free text.** The regex is the part most likely to
  need tuning once you see real listings. Always confirm weight against the
  hallmark/photos before bidding.
- **The API current bid can lag the live eBay page.** Always verify the real bid
  on site before bidding.
- **The bid is not your true cost** — postage and any buyer's premium are extra
  and are *not* included in the melt comparison.
