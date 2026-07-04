# eBay Gold Ring Scanner

A single-file command-line tool that scans **16 eBay marketplaces** (UK, US,
Europe, AU/CA, HK/SG) for one thing only: **heavy solid-gold signet and
intaglio rings with a seller-stated weight of 15g or more** (any carat 8–24ct).
Each ring's price (converted to £) is compared against its gold **melt value**
(carat × weight × live spot) and the true **landed cost** to a UK buyer
(import VAT + postage). No estimates, no review lane, no other styles — if the
seller didn't state a weight inside the window, it isn't shown. The window and
style targets are single constants (`WEIGHT_FLOOR_CONFIRMED`,
`WEIGHT_CEILING_CONFIRMED`, `REQUIRED_TAGS`, `STRICT_CONFIRMED_ONLY`).

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
| `--query` | `9ct gold signet ring` | eBay search term(s); combine several with `\|\|` and they're merged & de-duplicated |
| `--buying` | `both` | `auction`, `fixed` (Buy It Now), or `both` |
| `--max-price` | `250` | Max current price (GBP) to consider |
| `--threshold` | `1.3` | Flag if `bid < melt × threshold` |
| `--limit` | `200` | Max listings to fetch (politeness cap) |
| `--csv` | `gold_ring_results.csv` | Output CSV path |

All other tunables live in the clearly-commented **CONFIG** block at the top of
`gold_ring_scanner.py`.

## Coverage model (signets & intaglios, 15g+, confirmed weight)

**Strict mode is the default** (`STRICT_CONFIRMED_ONLY = True`,
`REQUIRED_TAGS = ("signet", "intaglio")`): a listing is kept only when it is a
signet/intaglio AND its parsed, seller-stated net gold weight sits inside
[`WEIGHT_FLOOR_CONFIRMED`, `WEIGHT_CEILING_CONFIRMED`] = **15g and up** (ceiling at the 80g parse-sanity cap). The value
flag is the core economic test: `landed_cost < melt_value × 1.3`. The tiers
below describe the machinery; in strict mode the ESTIMATED and UNKNOWN tiers
are dropped rather than kept.

### Weight-confidence model

Weight is seller-claimed free text, so a single hard weight filter either drops
good rings or lets light ones through. Instead every ring gets a **confidence
tier**, and the gram floor is applied per-tier (see `assess_ring`):

| Tier | Meaning | Kept when | Can flag "value"? |
|------|---------|-----------|--------------------|
| **CONFIRMED** | a weight was parsed from the title (title wins over description figures) | net gold ≥ `WEIGHT_FLOOR_CONFIRMED` (default **15 g**, strict) | **Yes — the only tier that can** |
| **ESTIMATED** | no stated weight, but an explicit weight adjective (heavy/chunky/…) plus archetype × carat density × size gives a lower-bound | net gold ≥ `WEIGHT_FLOOR_ESTIMATED_LOWBOUND` (default **12 g**) | No — ratio shown as indicative (`~`) only |
| **UNKNOWN** | no weight and no weight adjective (a plain "signet ring" carries no weight information) | **always kept**, routed to the dashboard **review lane** | No — never melt-valued |

A buy signal never rests on a guessed weight: a live-run audit showed
archetype-only guesses (and "solid"/"massiv", which mean *not plated*, not
*heavy*) flagging small ladies' rings as bargains. Estimation now requires an
explicit weight adjective, and `is_value` requires a CONFIRMED weight. Ratios
above `SUSPECT_RATIO` (2.5× melt) are demoted as too-good-to-be-true — nobody
knowingly sells at <40% of scrap; those are misparses, lots, or scams.
Multi-variant listings (one page, many rings) go to the review lane: the API
price is the cheapest variant, so the melt comparison is meaningless.
Listings naming **two or more different gem families** (any language) are
rejected as stone showcases — the gold is the mount, not the mass.

Net gold = parsed/estimated gross weight **minus a conservative stone
allowance** (`STONE_ALLOWANCE_G`, larger `INTAGLIO_ALLOWANCE_G` for carved
hardstone seals). Gold-light "showcase" pieces (cluster/halo/eternity/cocktail/
dress rings) are rejected outright (`STONE_SHOWCASE_WORDS`), but a stone-set
signet/intaglio can still qualify on its *net* gold.

### Search-term matrix (localised + English)

`queries_for(market, carat)` builds a per-market query list: the marketplace's
own language and **fineness mark** (DE `Siegelring 750`, FR `chevalière or 916`,
IT `anello oro 585`, ES `sello oro`, NL `zegelring goud`, PL `sygnet złoto`, …)
**plus** the English terms in parallel on every market (cross-border sellers).
Parsing understands multilingual carat/fineness marks (375/585/750/916/917/833/
333), **European decimal commas** (`15,3 g`), and unit variants
(`g`/`gr`/`gm`/`grams`/`grammi`/`Gramm`). Results are de-duplicated globally by
eBay item ID. Condition coverage is used **and** new **and** for-parts (scrap),
auction **and** Buy-It-Now (`--conditions ANY`).

### Key tunables (top of `gold_ring_scanner.py`)

| Constant | Default | Meaning |
|----------|---------|---------|
| `WEIGHT_FLOOR_CONFIRMED` | `15.0` | strict gram floor for a *parsed* weight (also set by `--min-weight`) |
| `WEIGHT_FLOOR_ESTIMATED_LOWBOUND` | `12.0` | gram floor for an *estimated* low-bound |
| `ARCHETYPE_LOW_G` | — | conservative low-bound grams per ring archetype (at 18ct) |
| `RING_DENSITY` | — | alloy density per carat; estimates scale with this |
| `STONE_ALLOWANCE_G` / `INTAGLIO_ALLOWANCE_G` | `1.5` / `3.5` | grams subtracted for stones before the floor test |
| `STONE_SHOWCASE_WORDS` | — | gold-light pieces rejected outright |
| `MAX_QUERIES_PER_MARKET` | `7` | caps the query matrix so the eBay quota lasts a full run |
| `MAX_DETAIL_FETCHES` | `250` | full-description look-ups per carat scan (each can turn an unknown into a confirmed weight) |
| `MELT_THRESHOLD` | `1.3` | value flag: `landed_cost < melt × threshold` (confirmed weight only) |
| `SUSPECT_RATIO` | `2.5` | melt/landed above this is demoted as too-good-to-be-true |

Every scan prints a **per-market yield log** (`raw → kept → value`), a
weight-confidence tally, and the eBay API call count, so coverage leaks and
quota spend are visible.

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

- The `Update gold ring scan` workflow runs daily (08:00 UTC, after eBay's
  ~07:00 UTC quota reset) and commits fresh `data/*.json`. Scheduled runs only
  fire from the **default branch** — merge this branch in to activate the schedule.
- You can also run it any time from the **Actions** tab ("Run workflow").

## Important caveats

- **Weights are seller-claimed free text.** The regex is the part most likely to
  need tuning once you see real listings. Always confirm weight against the
  hallmark/photos before bidding.
- **The API current bid can lag the live eBay page.** Always verify the real bid
  on site before bidding.
- **The bid is not your true cost** — postage and any buyer's premium are extra
  and are *not* included in the melt comparison.
