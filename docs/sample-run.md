# Sample run log — coverage expansion

A small two-market validation run (`EBAY_GB,EBAY_DE`, 9ct, `--limit 60`) showing
the new per-market **yield funnel** (`raw → kept → value`), the weight-confidence
tally, and eBay API-call instrumentation. The production GitHub Action runs all
**16 markets × 4 carats** at `--limit 250`, so absolute counts are far higher;
this excerpt is just to show the funnel and the new logging.

```
eBay Gold Ring Scanner
  carat=9  buying=both  conditions=ANY  price=£350-£6000  floors: confirmed>=15.0g estimated>=12.0g  limit=60/mkt  markets=2
  gold spot: £3,045.75/oz  (source: gold-api.com x frankfurter (live, $4,027/oz))
  fx to GBP: AUD=0.521, CAD=0.531, CHF=0.934, EUR=0.862, HKD=0.096, PLN=0.201, SGD=0.584, USD=0.756
  EBAY_GB: 7q -> 60 new listing(s)
  EBAY_DE: 7q -> 60 new listing(s)
  fetched 120 unique listing(s) across 2 market(s).
  ...
  per-market yield  raw -> kept -> value:
    DE    60 ->   2 ->  0
    UK    60 ->   1 ->  0
  weight confidence: confirmed=1 estimated=0 unknown=2
  eBay API calls: search=2 item=79
```

## How to read it

- **7q** per market = the auto-built localised + English query matrix
  (`queries_for`). On DE that includes `Siegelring 375`, `Herrenring massiv 375`,
  etc., plus the English `9ct …` terms; on GB it is the English set.
- **raw → kept → value**: raw listings fetched, kept after the solid-gold +
  weight-confidence filters, then flagged "value" (`landed_cost < melt × 1.3`).
  The big raw→kept drop is intentional — sub-15g rings are filtered out (the
  user only wants heavy rings); coverage is widened by *markets, queries and
  conditions*, not by keeping light rings.
- **weight confidence**: `confirmed` (parsed weight ≥15g), `estimated`
  (archetype low-bound ≥12g), `unknown` (no weight signal — kept in the review
  lane, never melt-valued). Here the two German listings had no parseable weight
  and no archetype keyword, so they land in the review lane rather than being
  dropped.
- **eBay API calls**: `search` (one per market) + `item` (full-description
  look-ups for weight-unknown listings, bounded by `MAX_DETAIL_FETCHES`) — quota
  spend is now visible in every run.

One example kept listing (the heavy signet that earlier versions missed):

```
conf=confirmed  gross=19.67g  net=19.7g  tags=[signet, vintage]  stones=False
title=9CT YELLOW GOLD HEAVY SIGNET RING, SIZE S. 19.67 GRAMS
```
