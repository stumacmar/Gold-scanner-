#!/usr/bin/env python3
"""
================================================================================
 eBay Vintage Gold Signet Ring Scanner  (gold_ring_scanner.py)
================================================================================

Finds undervalued vintage gold signet rings on eBay UK by comparing the live
auction bid against the ring's gold *melt* value (carat content x weight x spot).

--------------------------------------------------------------------------------
WHAT IT DOES
--------------------------------------------------------------------------------
 1. Queries the eBay Browse API (buy/browse/v1/item_summary/search) for UK
    auctions matching a search term (default "9ct gold signet ring").
 2. Filters to AUCTION + USED + EBAY_GB under a configurable max price.
 3. Parses the gold WEIGHT (grams) and CARAT from the title / description.
 4. Skips plated / rolled / gold-tone junk.
 5. Pulls the live gold spot price (metals.dev or goldapi.io, or a hardcoded
    fallback) and computes each ring's melt value.
 6. Flags auctions where current_bid < melt x threshold (default 1.3) as
    "VALUE" candidates, sorted best-value-first.
 7. Prints a table to the console and writes the full result set to CSV.

--------------------------------------------------------------------------------
HOW TO GET eBay API CREDENTIALS  (free, ~5 minutes)
--------------------------------------------------------------------------------
 1. Go to https://developer.ebay.com and sign in (create an account if needed).
 2. Open the Developer Account > "Application Keys" page.
 3. Create an application keyset. You want the **Production** keyset (not
    Sandbox) so you get real live listings.
 4. Copy the "App ID (Client ID)" and "Cert ID (Client Secret)".
 5. The Browse API uses an OAuth2 *application* token (client-credentials),
    NOT a user token. This script fetches and caches that token for you.
 6. Optionally get a gold-price key from https://metals.dev or
    https://goldapi.io (both have free tiers). Without one, the script uses
    the SPOT_PRICE_GBP_PER_OZ fallback constant below.

--------------------------------------------------------------------------------
SETUP
--------------------------------------------------------------------------------
    pip install requests python-dotenv

 Create a .env file next to this script (see .env.example):

    EBAY_CLIENT_ID=YourAppId
    EBAY_CLIENT_SECRET=YourCertId
    # optional:
    METALS_API_KEY=...        # for metals.dev
    GOLDAPI_KEY=...           # for goldapi.io

--------------------------------------------------------------------------------
RUN
--------------------------------------------------------------------------------
    python gold_ring_scanner.py
    python gold_ring_scanner.py --query "18ct gold signet ring" --max-price 400
    python gold_ring_scanner.py --threshold 1.0 --limit 100

--------------------------------------------------------------------------------
IMPORTANT CAVEATS  (read before bidding!)
--------------------------------------------------------------------------------
 * Weight parsing is regex-based against messy free text. Treat every weight as
    "claimed" until you verify it against the photos/hallmark. This is the part
    most likely to need tuning after you see real listings.
 * The Browse API's current bid can lag the live page by a short while. ALWAYS
    verify the real current bid on the eBay site before bidding.
 * Buyer costs are higher than the bid: postage and any buyer's premium are NOT
    included in melt comparisons.
================================================================================
"""

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("[warn] python-dotenv not installed; relying on real environment "
          "variables. Install with: pip install python-dotenv", file=sys.stderr)

# =============================================================================
# CONFIG  -- tune everything here
# =============================================================================

# --- Search ---------------------------------------------------------------
SEARCH_TERM       = "9ct gold signet ring"   # default eBay search query
MARKETPLACE       = "EBAY_GB"                 # UK marketplace
PRICE_CURRENCY    = "GBP"                     # currency for the price filter
MAX_CURRENT_PRICE = 250.0                     # only consider auctions at/under this (GBP)
MAX_ITEMS         = 200                       # hard cap on items fetched (be polite)
PAGE_SIZE         = 200                       # Browse API max page size is 200

# --- Valuation ------------------------------------------------------------
MELT_THRESHOLD    = 1.3    # flag auctions where current_bid < melt * threshold.
                           #   1.0  = bid must be at/under pure melt value
                           #   1.3  = allow bids up to 30% over melt (default;
                           #          a permissive net to catch near-melt rings,
                           #          since you still profit room below resale).

# Gold spot fallback, used ONLY if no price-API key is configured or the API
# call fails. Edit this to roughly today's gold price in GBP per TROY OUNCE of
# pure (24ct / .999) gold.  ~ as of mid-2026; UPDATE BEFORE RELYING ON IT.
SPOT_PRICE_GBP_PER_OZ = 1850.0

# Which live price provider to use:
#   "free"       -> gold-api.com x Frankfurter FX (no API key needed; default)
#   "metals.dev" -> needs METALS_API_KEY
#   "goldapi.io" -> needs GOLDAPI_KEY
#   "none"       -> always use the SPOT_PRICE_GBP_PER_OZ fallback below
SPOT_PROVIDER = "free"

TROY_OZ_IN_GRAMS = 31.1034768   # 1 troy ounce = 31.1034768 g

# --- Carat (millesimal fineness) -----------------------------------------
# Maps detected carat -> fraction of pure gold.
CARAT_FRACTION = {
    9:  375 / 1000,
    10: 417 / 1000,
    14: 585 / 1000,
    15: 625 / 1000,
    18: 750 / 1000,
    22: 916.6 / 1000,
    24: 999 / 1000,
}
DEFAULT_CARAT = 9   # assume 9ct when an item is clearly gold but no carat found

# --- Detail fetching ------------------------------------------------------
# The search endpoint only returns a short description. If the weight isn't
# found there, optionally call the getItem endpoint for the FULL description.
# This costs one extra API call per weight-unknown item (still bounded by
# MAX_ITEMS), so it's worth it but can be disabled for speed.
FETCH_FULL_DETAILS = True

# --- Output ---------------------------------------------------------------
CSV_PATH       = "gold_ring_results.csv"
TITLE_TRUNCATE = 45   # title column width in the console table

# --- eBay endpoints -------------------------------------------------------
OAUTH_URL   = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL  = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL    = "https://api.ebay.com/buy/browse/v1/item/{item_id}"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
TOKEN_CACHE = ".ebay_token_cache.json"

# =============================================================================
# Words that mean "this is NOT solid gold" -> skip the listing.
# =============================================================================
PLATED_MARKERS = [
    "plated", "gold plated", "gold-plated", "rolled gold", "rolled-gold",
    "gold tone", "gold-tone", "gold filled", "gold-filled", "gold fill",
    "gold colour", "gold color", "gold coloured", "gold colored",
    "gilt", "gilded", "vermeil", "epns", "gp ", " gp", "rgp", "1/20",
    "plate ", "electroplated", "gold effect", "gold plate", "costume",
    # Vintage "gold-fronted silver" -- a thin gold layer over a silver body.
    # The bulk metal is silver, so weight-based melt is meaningless. Skip.
    "gold on silver", "gold on sterling", "gold & silver", "gold and silver",
    "silver & gold", "silver and gold", "gold backed silver",
    "gold fronted", "silver gilt", "silver lined", "silver-lined",
    "sterling silver", "silver shank", "silver sleeve",
    # Gold-fronted/plated base metals -- weight is mostly the base metal.
    "gold on brass", "gold and brass", "gold & brass", "on brass", "brass",
    "gold on copper", "base metal", "pinchbeck",
]

# =============================================================================
# OAuth2 (client-credentials application token) with on-disk caching
# =============================================================================

def get_ebay_token():
    """Return a valid eBay application access token, fetching/caching as needed."""
    # Reuse a cached token if it still has comfortable headroom.
    if os.path.exists(TOKEN_CACHE):
        try:
            with open(TOKEN_CACHE) as fh:
                cached = json.load(fh)
            if cached.get("expires_at", 0) - 60 > time.time():
                return cached["access_token"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass  # cache unreadable; fetch fresh

    # .strip() guards against trailing spaces/newlines accidentally pasted into
    # a .env file or a GitHub Actions secret -- a common cause of 401s.
    client_id = (os.getenv("EBAY_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("EBAY_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        sys.exit("[fatal] EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set. "
                 "Add them to a .env file (see .env.example).")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials", "scope": OAUTH_SCOPE}

    resp = requests.post(OAUTH_URL, headers=headers, data=data, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"[fatal] eBay OAuth failed ({resp.status_code}): {resp.text}")

    payload = resp.json()
    token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 7200))
    try:
        with open(TOKEN_CACHE, "w") as fh:
            json.dump({"access_token": token,
                       "expires_at": time.time() + expires_in}, fh)
    except OSError:
        pass  # caching is best-effort
    return token

# =============================================================================
# Text parsing: weight, carat, plated detection
# =============================================================================

# Number (allowing comma or dot decimal) immediately followed by a gram unit.
# Handles: 9.4g  9.4 g  9.4grams  9.4 gram  9.4gm  9.4 gms  9,4 g  9.4gr
_WEIGHT_RE = re.compile(
    r"(\d{1,3}(?:[.,]\d{1,2})?)\s*"
    r"(?:grammes?|grams?|gms?|gm|grm?s?|gr|g)\b",
    re.IGNORECASE,
)

# Carat patterns -> carat number. Checked in order; first hit wins per group.
_CARAT_PATTERNS = [
    (24, re.compile(r"\b24\s*c?t\b|\b24\s*k(?:t)?\b|\b24\s*carat\b|\b999\b", re.I)),
    (22, re.compile(r"\b22\s*c?t\b|\b22\s*k(?:t)?\b|\b22\s*carat\b|\b916\b|\b917\b", re.I)),
    (18, re.compile(r"\b18\s*c?t\b|\b18\s*k(?:t)?\b|\b18\s*carat\b|\b750\b", re.I)),
    (15, re.compile(r"\b15\s*c?t\b|\b15\s*k(?:t)?\b|\b15\s*carat\b|\b625\b", re.I)),
    (14, re.compile(r"\b14\s*c?t\b|\b14\s*k(?:t)?\b|\b14\s*carat\b|\b585\b", re.I)),
    (10, re.compile(r"\b10\s*c?t\b|\b10\s*k(?:t)?\b|\b10\s*carat\b|\b417\b", re.I)),
    (9,  re.compile(r"\b9\s*c?t\b|\b9\s*k(?:t)?\b|\b9\s*carat\b|\b375\b", re.I)),
]


def parse_weight_grams(text):
    """Return the most plausible gold weight in grams, or None if not found.

    Picks the LARGEST matched gram figure on the assumption that it's the total
    item weight (sellers sometimes also quote tiny stone weights). Real-world
    listings vary wildly, so expect to tune this after a first run.
    """
    if not text:
        return None
    candidates = []
    for m in _WEIGHT_RE.finditer(text):
        raw = m.group(1).replace(",", ".")
        try:
            grams = float(raw)
        except ValueError:
            continue
        # Sanity bounds: a signet ring is realistically ~1g to ~60g.
        if 0.3 <= grams <= 100:
            candidates.append(grams)
    if not candidates:
        return None
    return max(candidates)


def detect_carat(text):
    """Return (carat, assumed_flag). assumed_flag=True when we fell back to default."""
    if not text:
        return DEFAULT_CARAT, True
    for carat, pattern in _CARAT_PATTERNS:
        if pattern.search(text):
            return carat, False
    return DEFAULT_CARAT, True


def is_plated(text):
    """True if the text strongly suggests plated / rolled / non-solid gold."""
    if not text:
        return False
    low = f" {text.lower()} "
    return any(marker in low for marker in PLATED_MARKERS)

# =============================================================================
# Gold spot price
# =============================================================================

def _free_spot_gbp_per_oz():
    """Keyless live price: gold-api.com (USD/oz) x Frankfurter ECB (USD->GBP).

    Both are free and need no API key, so this works in CI with zero secrets.
    Returns (price_gbp_per_oz, label) or raises on failure.
    """
    g = requests.get("https://api.gold-api.com/price/XAU", timeout=30)
    g.raise_for_status()
    usd_per_oz = float(g.json()["price"])
    fx = requests.get("https://api.frankfurter.dev/v1/latest",
                      params={"base": "USD", "symbols": "GBP"}, timeout=30)
    fx.raise_for_status()
    usd_to_gbp = float(fx.json()["rates"]["GBP"])
    return usd_per_oz * usd_to_gbp, f"gold-api.com x frankfurter (live, ${usd_per_oz:,.0f}/oz)"


def get_spot_price_gbp_per_oz():
    """Return (price_per_troy_oz_GBP, source_label)."""
    provider = SPOT_PROVIDER.lower()

    if provider == "free":
        try:
            return _free_spot_gbp_per_oz()
        except (requests.RequestException, KeyError, ValueError, TypeError) as e:
            print(f"[warn] free price lookup failed ({e}); using fallback.",
                  file=sys.stderr)

    if provider == "metals.dev":
        key = os.getenv("METALS_API_KEY")
        if key:
            try:
                r = requests.get(
                    "https://api.metals.dev/v1/latest",
                    params={"api_key": key, "currency": "GBP", "unit": "toz"},
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                price = float(data["metals"]["gold"])
                return price, "metals.dev (live)"
            except (requests.RequestException, KeyError, ValueError, TypeError) as e:
                print(f"[warn] metals.dev lookup failed ({e}); using fallback.",
                      file=sys.stderr)

    elif provider == "goldapi.io":
        key = os.getenv("GOLDAPI_KEY")
        if key:
            try:
                r = requests.get(
                    "https://www.goldapi.io/api/XAU/GBP",
                    headers={"x-access-token": key, "Content-Type": "application/json"},
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                price = float(data["price"])  # price per troy ounce
                return price, "goldapi.io (live)"
            except (requests.RequestException, KeyError, ValueError, TypeError) as e:
                print(f"[warn] goldapi.io lookup failed ({e}); using fallback.",
                      file=sys.stderr)

    return SPOT_PRICE_GBP_PER_OZ, "hardcoded fallback constant"

# =============================================================================
# eBay Browse API: search + item detail
# =============================================================================

_BUYING_FILTER = {
    "auction": "AUCTION",
    "fixed": "FIXED_PRICE",
    "both": "AUCTION|FIXED_PRICE",
}


def search_listings(token, queries, max_price, max_items, buying="both"):
    """Search one or more queries, page through them, and merge results.

    De-duplicates by itemId so the same ring found under several search terms is
    only kept once. `buying` selects auctions, fixed-price (Buy It Now), or both.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
        "Content-Type": "application/json",
    }
    buy = _BUYING_FILTER.get(buying, _BUYING_FILTER["both"])
    # Browse API filter syntax. priceCurrency is required alongside a price range.
    item_filter = (
        f"buyingOptions:{{{buy}}},"
        "conditions:{USED},"
        f"price:[..{max_price}],"
        f"priceCurrency:{PRICE_CURRENCY}"
    )

    seen = set()
    items = []
    for query in queries:
        offset = 0
        while len(items) < max_items:
            page = min(PAGE_SIZE, max_items - len(items))
            params = {
                "q": query,
                "filter": item_filter,
                "limit": page,
                "offset": offset,
                "fieldgroups": "EXTENDED",   # include shortDescription
            }
            # endingSoonest only applies to auctions; let other modes use
            # eBay's default best-match relevance ordering.
            if buying == "auction":
                params["sort"] = "endingSoonest"
            r = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
            if r.status_code != 200:
                print(f"[warn] search '{query}' failed ({r.status_code}): {r.text}",
                      file=sys.stderr)
                break
            data = r.json()
            batch = data.get("itemSummaries", []) or []
            for it in batch:
                iid = it.get("itemId")
                if iid and iid in seen:
                    continue
                if iid:
                    seen.add(iid)
                items.append(it)
            total = data.get("total", 0)
            offset += page
            if not batch or offset >= total or len(items) >= max_items:
                break
            time.sleep(0.2)  # be polite between pages
        if len(items) >= max_items:
            break

    return items[:max_items]


def fetch_item_description(token, item_id):
    """Return the full description text for an item, or '' on failure."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
    }
    try:
        r = requests.get(ITEM_URL.format(item_id=item_id),
                         headers=headers, timeout=30)
        if r.status_code != 200:
            return ""
        data = r.json()
        # Description is HTML; strip tags down to plain text for regex parsing.
        desc = data.get("description", "") or ""
        desc = re.sub(r"<[^>]+>", " ", desc)
        # Also fold in localized aspects (often hold "Metal Purity: 9ct" etc.)
        aspects = []
        for grp in data.get("localizedAspects", []) or []:
            aspects.append(f"{grp.get('name','')} {grp.get('value','')}")
        return desc + " " + " ".join(aspects)
    except requests.RequestException:
        return ""

# =============================================================================
# Helpers
# =============================================================================

def get_current_bid(item):
    """Return the current price/bid (GBP float), or None.

    Auctions expose currentBidPrice; fixed-price (Buy It Now) uses price.
    """
    bid = item.get("currentBidPrice") or item.get("price")
    if not bid:
        return None
    try:
        return float(bid.get("value"))
    except (TypeError, ValueError):
        return None


def buying_type(item):
    """Return 'Auction', 'Buy now', or 'Auction+Offer' style label."""
    opts = item.get("buyingOptions") or []
    if "AUCTION" in opts:
        return "Auction"
    if "FIXED_PRICE" in opts or "BEST_OFFER" in opts:
        return "Buy now"
    return "?"


def time_left(end_iso):
    """Human-readable time remaining from an ISO8601 end date, or 'n/a'."""
    if not end_iso:
        return "n/a"
    try:
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError:
        return "n/a"
    delta = end - datetime.now(timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "ended"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def truncate(text, width):
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"

# =============================================================================
# Core analysis
# =============================================================================

def analyse(items, token, spot_per_oz):
    """Turn raw eBay summaries into analysed ring records."""
    spot_per_gram_fine = spot_per_oz / TROY_OZ_IN_GRAMS
    records = []

    for item in items:
        title = item.get("title", "")
        short_desc = item.get("shortDescription", "") or ""
        text = f"{title} {short_desc}"

        # 1. Reject obvious non-solid-gold listings outright.
        if is_plated(text):
            continue

        # 2. Carat + weight from summary text.
        carat, carat_assumed = detect_carat(text)
        weight = parse_weight_grams(text)

        # 3. If weight still unknown, optionally dig into the full description.
        if weight is None and FETCH_FULL_DETAILS:
            full = fetch_item_description(token, item.get("itemId", ""))
            if full:
                if is_plated(full):
                    continue
                weight = parse_weight_grams(full)
                if carat_assumed:
                    carat, carat_assumed = detect_carat(full)
            time.sleep(0.1)  # polite pacing for the extra call

        bid = get_current_bid(item)
        fraction = CARAT_FRACTION.get(carat, CARAT_FRACTION[DEFAULT_CARAT])
        content_per_gram = spot_per_gram_fine * fraction

        melt = round(weight * content_per_gram, 2) if weight else None
        ratio = round(melt / bid, 2) if (melt and bid) else None  # melt-to-bid

        # VALUE flag: bid below melt * threshold (see MELT_THRESHOLD comment).
        is_value = bool(melt and bid and bid < melt * MELT_THRESHOLD)

        records.append({
            "title": title,
            "carat": carat,
            "carat_assumed": carat_assumed,
            "weight_g": weight,
            "current_bid": bid,
            "melt_value": melt,
            "ratio": ratio,
            "is_value": is_value,
            "buying": buying_type(item),
            "time_left": time_left(item.get("itemEndDate")),
            "bids": item.get("bidCount", ""),
            "url": item.get("itemWebUrl", ""),
        })

    return records

# =============================================================================
# Output
# =============================================================================

def print_table(records):
    value = [r for r in records if r["is_value"]]
    # Best value first = highest melt-to-bid ratio.
    value.sort(key=lambda r: (r["ratio"] is not None, r["ratio"] or 0),
               reverse=True)

    print()
    print("=" * 110)
    print(f"  VALUE CANDIDATES  (bid < melt x {MELT_THRESHOLD})  -- "
          f"{len(value)} of {len(records)} analysed listings")
    print("=" * 110)

    if not value:
        print("  No value candidates found. Try raising --max-price, widening "
              "--query, or relaxing --threshold.")
    else:
        header = (f"{'Title':<46}{'Carat':<7}{'Wt(g)':<7}{'Bid':<9}"
                  f"{'Melt':<9}{'M/Bid':<7}{'Left':<8}")
        print(header)
        print("-" * 110)
        for r in value:
            carat = f"{r['carat']}ct" + ("?" if r["carat_assumed"] else "")
            wt = f"{r['weight_g']:.1f}" if r["weight_g"] else "?"
            bid = f"£{r['current_bid']:.0f}" if r["current_bid"] else "?"
            melt = f"£{r['melt_value']:.0f}" if r["melt_value"] else "?"
            ratio = f"{r['ratio']:.2f}" if r["ratio"] else "?"
            print(f"{truncate(r['title'], 45):<46}{carat:<7}{wt:<7}{bid:<9}"
                  f"{melt:<9}{ratio:<7}{r['time_left']:<8}")
            print(f"    -> {r['url']}")

    # Weight-unknown items are worth a manual glance; surface a count + hint.
    unknown = [r for r in records if r["weight_g"] is None]
    if unknown:
        print("-" * 110)
        print(f"  {len(unknown)} listing(s) had NO parseable weight "
              f"(see CSV, 'weight unknown'). Worth a manual look.")

    print("=" * 110)
    print("  !! VERIFY ON SITE BEFORE BIDDING: the API current bid can lag the "
          "live eBay page.")
    print("  !! Bid shown is NOT your true cost -- buyer's premium (if any) and "
          "POSTAGE are extra.")
    print("  !! Weights are seller-claimed text; confirm against hallmark/photos.")
    print("=" * 110)
    print()


def write_json(records, path, meta):
    """Write records + run metadata to JSON for the web dashboard."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {"meta": meta, "results": records}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f"  Dashboard data written to: {path}")


def write_csv(records, path):
    fields = ["title", "carat", "carat_assumed", "weight_g", "current_bid",
              "melt_value", "ratio", "is_value", "bids", "time_left", "url"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for r in records:
            row = {k: r.get(k) for k in fields}
            if row["weight_g"] is None:
                row["weight_g"] = "weight unknown"
            writer.writerow(row)
    print(f"  Full results ({len(records)} rows) written to: {path}")

# =============================================================================
# Main
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Scan eBay UK auctions for undervalued gold signet rings.")
    p.add_argument("--query", default=SEARCH_TERM,
                   help="eBay search term(s); separate multiple with '||' and "
                        "they are merged & de-duplicated")
    p.add_argument("--buying", default="both", choices=["auction", "fixed", "both"],
                   help="auction only, fixed-price (Buy It Now) only, or both")
    p.add_argument("--max-price", type=float, default=MAX_CURRENT_PRICE,
                   help="max current price (GBP) to consider")
    p.add_argument("--threshold", type=float, default=MELT_THRESHOLD,
                   help="flag if bid < melt * threshold")
    p.add_argument("--limit", type=int, default=MAX_ITEMS,
                   help="max listings to fetch (cap, be polite)")
    p.add_argument("--csv", default=CSV_PATH, help="output CSV path")
    p.add_argument("--json", default="data/results.json",
                   help="output JSON path for the web dashboard")
    p.add_argument("--default-carat", type=int, default=DEFAULT_CARAT,
                   choices=sorted(CARAT_FRACTION), metavar="CT",
                   help="carat to assume when none is detected (e.g. 18 for an "
                        "18ct search)")
    return p.parse_args()


def main():
    global MELT_THRESHOLD, DEFAULT_CARAT
    args = parse_args()
    MELT_THRESHOLD = args.threshold
    DEFAULT_CARAT = args.default_carat

    queries = [q.strip() for q in args.query.split("||") if q.strip()]

    print("\neBay Gold Ring Scanner")
    print(f"  queries={queries}  buying={args.buying}  "
          f"max_price=£{args.max_price:.0f}  threshold={args.threshold}  "
          f"limit={args.limit}  market={MARKETPLACE}")

    spot, source = get_spot_price_gbp_per_oz()
    print(f"  gold spot: £{spot:,.2f}/oz  (source: {source})")

    token = get_ebay_token()
    items = search_listings(token, queries, args.max_price, args.limit, args.buying)
    print(f"  fetched {len(items)} unique listing(s) from eBay.")

    records = analyse(items, token, spot)
    print_table(records)
    write_csv(records, args.csv)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": queries[0] if queries else args.query,
        "queries": queries,
        "buying": args.buying,
        "default_carat": args.default_carat,
        "marketplace": MARKETPLACE,
        "max_price": args.max_price,
        "threshold": args.threshold,
        "spot_gbp_per_oz": spot,
        "spot_source": source,
        "carat_per_gram": {str(k): round(spot / TROY_OZ_IN_GRAMS * v, 2)
                           for k, v in CARAT_FRACTION.items()},
        "total_fetched": len(items),
        "total_analysed": len(records),
        "value_count": sum(1 for r in records if r["is_value"]),
        "weight_unknown_count": sum(1 for r in records if r["weight_g"] is None),
    }
    write_json(records, args.json, meta)


if __name__ == "__main__":
    main()
