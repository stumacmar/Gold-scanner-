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
MAX_CURRENT_PRICE = 250.0                     # only consider auctions at/under this (GBP)
MAX_ITEMS         = 200                       # hard cap on items fetched per market

# eBay marketplaces we can scan. Prices are converted to GBP via live FX so the
# melt comparison is apples-to-apples. NOTE: non-UK buys carry import VAT/duty +
# international postage that the melt value does NOT include.
MARKETPLACES = {
    "EBAY_GB": {"currency": "GBP", "country": "UK", "flag": "🇬🇧"},
    "EBAY_US": {"currency": "USD", "country": "US", "flag": "🇺🇸"},
    "EBAY_IE": {"currency": "EUR", "country": "IE", "flag": "🇮🇪"},
    "EBAY_AU": {"currency": "AUD", "country": "AU", "flag": "🇦🇺"},
    "EBAY_CA": {"currency": "CAD", "country": "CA", "flag": "🇨🇦"},
    "EBAY_DE": {"currency": "EUR", "country": "DE", "flag": "🇩🇪"},
    "EBAY_FR": {"currency": "EUR", "country": "FR", "flag": "🇫🇷"},
    "EBAY_IT": {"currency": "EUR", "country": "IT", "flag": "🇮🇹"},
    "EBAY_ES": {"currency": "EUR", "country": "ES", "flag": "🇪🇸"},
    "EBAY_NL": {"currency": "EUR", "country": "NL", "flag": "🇳🇱"},
    "EBAY_AT": {"currency": "EUR", "country": "AT", "flag": "🇦🇹"},
    "EBAY_CH": {"currency": "CHF", "country": "CH", "flag": "🇨🇭"},
    "EBAY_BE": {"currency": "EUR", "country": "BE", "flag": "🇧🇪"},
    "EBAY_PL": {"currency": "PLN", "country": "PL", "flag": "🇵🇱"},
    "EBAY_HK": {"currency": "HKD", "country": "HK", "flag": "🇭🇰"},
    "EBAY_SG": {"currency": "SGD", "country": "SG", "flag": "🇸🇬"},
}
DEFAULT_MARKETS = "EBAY_GB"                   # comma-separated; overridden by --markets

# --- Localised search terms ----------------------------------------------
# Sellers abroad list by fineness mark (585/750...), not "ct", and in their own
# language. We run localised terms on each market PLUS English (cross-border
# sellers exist). Templates use {c}=carat number, {f}=fineness mark.
LOCAL_FINENESS = {8: "333", 9: "375", 14: "585", 15: "625",
                  18: "750", 20: "833", 22: "916"}
MARKET_LANG = {
    "EBAY_DE": "de", "EBAY_AT": "de", "EBAY_CH": "de",
    "EBAY_FR": "fr", "EBAY_BE": "fr",
    "EBAY_IT": "it", "EBAY_ES": "es", "EBAY_NL": "nl", "EBAY_PL": "pl",
}  # everything else defaults to English
# Signet/intaglio-focused: every kept listing must be one anyway, so the
# fetch budget goes entirely on the target style.
QUERY_TEMPLATES = {
    "en": ["{c}ct gold signet ring", "{c}ct heavy gold signet ring",
           "{c}ct gold intaglio ring", "{c}ct gold seal ring",
           "vintage {c}ct gold signet ring", "mens {c}ct gold signet ring"],
    "de": ["Siegelring Gold {f}", "Siegelring {f} Herren",
           "Intaglio Ring Gold {f}", "Siegelring Gold {f} schwer"],
    "fr": ["chevalière or {f}", "chevalière or {f} homme",
           "bague intaille or {f}"],
    "it": ["anello sigillo oro {f}", "anello chevalier oro {f}",
           "anello intaglio oro {f}", "anello sigillo oro {f} uomo"],
    "es": ["anillo sello oro {f}", "sello oro {f} hombre",
           "anillo intaglio oro {f}"],
    "nl": ["zegelring goud {f}", "heren zegelring goud {f}",
           "gouden zegelring {f}"],
    "pl": ["sygnet złoto {f}", "sygnet męski złoto {f}", "złoty sygnet {f}"],
}
MAX_QUERIES_PER_MARKET = 7    # cap the matrix so the API quota lasts a full run

# --- Silver mode (--metal silver) -----------------------------------------
# Second screen: heavy STERLING SILVER signets/intaglios. Same strict rules
# (signet/intaglio only, seller-stated weight >= floor), but melt uses the
# live silver spot and the fineness mark (925 sterling, continental 800/835).
# Silver trades much further above melt than gold, so the value threshold is
# looser: flag when landed < melt x SILVER_MELT_THRESHOLD.
SILVER_WEIGHT_FLOOR = 15.0      # "heavy" floor for silver signets (grams)
SILVER_MELT_THRESHOLD = 2.0
SPOT_SILVER_GBP_PER_OZ = 36.0   # fallback if the live lookup fails; ~mid-2026
SILVER_FINENESS = {"958": 0.958, "925": 0.925, "900": 0.900,
                   "835": 0.835, "830": 0.830, "800": 0.800}
SILVER_QUERY_TEMPLATES = {
    "en": ["sterling silver signet ring heavy", "925 silver signet ring",
           "solid silver signet ring", "silver signet ring mens",
           "silver intaglio ring", "sterling silver seal ring"],
    "de": ["Siegelring Silber 925", "Siegelring Silber massiv",
           "925 Siegelring Herren", "Siegelring Sterlingsilber"],
    "fr": ["chevalière argent massif", "chevalière argent 925",
           "bague intaille argent"],
    "it": ["anello sigillo argento 925", "anello argento uomo sigillo",
           "anello intaglio argento"],
    "es": ["anillo sello plata 925", "sello plata de ley hombre"],
    "nl": ["zilveren zegelring 925", "zegelring zilver heren"],
    "pl": ["sygnet srebro 925", "sygnet męski srebro"],
}
# Silver-mode exclusions: plating/costume + anything that's actually GOLD
# (a gold signet mentioning "silver" must not leak into the silver screen).
SILVER_PLATED_MARKERS = [
    "plated", "plate ", "silver tone", "silver-tone", "epns", "costume",
    "vermeil", "gilt", "gilded", "vergoldet", "gold plated", "gold-plated",
    "base metal", "stainless", "tungsten", "titanium", "brass",
]

# --- Yurman mode (--metal yurman) ------------------------------------------
# Third screen: David Yurman signet rings worldwide. A BRAND hunt, not a melt
# hunt: Yurman is mostly sterling or silver+18k two-tone (melt on the stated
# weight is fiction) and sellers rarely state weights, so there is no weight
# floor and no value flag -- price, seller type and country are the signals.
# Only listings claiming the genuine brand are kept; lookalikes are rejected.
YURMAN_QUERY_TEMPLATES = {          # brand names aren't translated -- same
    "en": ["David Yurman signet ring", "David Yurman mens ring",
           "David Yurman pinky ring", "Yurman signet ring",
           "David Yurman ring men",
           # feeds the Meteorite page (a filtered view of this dataset)
           "David Yurman meteorite ring", "David Yurman meteorite signet"],
}
YURMAN_FAKE_MARKERS = [             # "genuine brand claims only"
    "yurman style", "style of", "in the style", "inspired", "dupe",
    "replica", "repro", "reproduction", "faux", "copy", "like yurman",
    "similar to", "compare to", "unbranded", "not yurman", "dy style",
    "homage", "lookalike", "look-alike",
]


def queries_for(market, carat, extra=(), metal="gold"):
    """Build the de-duplicated localised+English query list for a market/carat."""
    lang = MARKET_LANG.get(market, "en")
    if metal == "yurman":
        templates, lang = YURMAN_QUERY_TEMPLATES, "en"
    elif metal == "silver":
        templates = SILVER_QUERY_TEMPLATES
    else:
        templates = QUERY_TEMPLATES
    f = LOCAL_FINENESS.get(carat, str(carat))
    terms = [t.format(c=carat, f=f) for t in templates.get(lang, [])]
    if lang != "en":                       # cross-border English sellers too
        terms += [t.format(c=carat, f=f) for t in templates["en"][:3]]
    terms += list(extra)
    seen, out = set(), []
    for q in terms:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:MAX_QUERIES_PER_MARKET]

# FX fallback (units of foreign currency per £1) if the live lookup fails.
FX_FALLBACK_PER_GBP = {"GBP": 1.0, "USD": 1.27, "EUR": 1.17, "AUD": 1.93,
                       "CAD": 1.74, "CHF": 1.13, "PLN": 5.0, "HKD": 10.3,
                       "SGD": 1.71}

# --- Landed cost (true cost to a UK buyer) --------------------------------
# Non-UK buys add international postage + UK import VAT (20% on goods+postage;
# precious-metal jewellery is 0% customs duty) + a courier handling fee.
# These are rough estimates -- edit to taste.
IMPORT_VAT_RATE = 0.20
IMPORT_HANDLING_FEE_GBP = 12          # typical courier "advancement" fee
UK_POSTAGE_GBP = 4                    # domestic tracked postage (approx)
POSTAGE_EST_GBP = {                   # est. international postage to the UK
    "US": 18, "CA": 18, "AU": 25, "CH": 16, "IE": 10,
    "DE": 12, "FR": 12, "IT": 12, "ES": 12, "NL": 12, "AT": 12, "BE": 12,
    "PL": 14, "HK": 20, "SG": 20, "MY": 25, "PH": 28, "IN": 25,
}


def landed_cost(price_gbp, country):
    """Estimate the true £ cost to a UK buyer (price + postage + import VAT)."""
    if price_gbp is None:
        return None
    if country == "UK":
        return round(price_gbp + UK_POSTAGE_GBP, 2)
    postage = POSTAGE_EST_GBP.get(country, 18)
    vat = IMPORT_VAT_RATE * (price_gbp + postage)
    return round(price_gbp + postage + vat + IMPORT_HANDLING_FEE_GBP, 2)
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
    8:  333 / 1000,   # continental 8ct (333) -- out of primary scope, for melt accuracy
    9:  375 / 1000,
    10: 417 / 1000,
    14: 585 / 1000,
    15: 625 / 1000,
    18: 750 / 1000,
    20: 833 / 1000,   # continental 20ct (833)
    22: 916.6 / 1000,
    24: 999 / 1000,
}
DEFAULT_CARAT = 9   # assume 9ct when an item is clearly gold but no carat found
METAL = "gold"      # gold | silver -- set by --metal; silver switches spot,
                    # fineness parsing, exclusions, and query templates

# --- Weight-confidence model (see assess_ring) ---------------------------
WEIGHT_FLOOR_CONFIRMED = 15.0          # strict floor for a parsed (stated) weight
WEIGHT_CEILING_CONFIRMED = 80.0        # effectively "15g and up" -- matches the
                                       # weight parser's sanity cap; lower this
                                       # one number to narrow the window
WEIGHT_FLOOR_ESTIMATED_LOWBOUND = 12.0 # estimated rings kept if low-bound >= this
                                       # (only used when STRICT_CONFIRMED_ONLY off)

# THE PRODUCT: heavy solid-gold signet/intaglio rings with a SELLER-STATED
# weight inside the window. Everything else is dropped -- no review lane, no
# estimates. Set REQUIRED_TAGS = () or STRICT_CONFIRMED_ONLY = False to widen.
STRICT_CONFIRMED_ONLY = True
REQUIRED_TAGS = ("signet", "intaglio")
# Approx density of gold alloy by carat (g/cm3); a fixed ring volume weighs more
# at higher carat, so weight estimates scale with this relative to the 18ct base.
RING_DENSITY = {8: 10.9, 9: 11.3, 10: 11.6, 14: 13.4, 15: 14.0,
                18: 15.5, 20: 16.5, 22: 17.8, 24: 19.3}
_DENSITY_BASE = 15.5
# Conservative LOWER-BOUND gram estimates per ring archetype (at 18ct), used only
# when no weight is stated. Only genuinely chunky archetypes clear the 12g floor.
ARCHETYPE_LOW_G = {
    # Tuned so a "heavy/chunky" signet clears the 12g estimated floor even at
    # 9ct (the least dense, most common carat: 17 * 11.3/15.5 ~ 12.4g). Plain
    # archetypes stay below the floor on purpose -> they fall to the review lane.
    "signet_heavy": 17.0, "signet": 9.0,
    "band_heavy": 13.0, "band": 6.0,
    "ring_heavy": 12.0,
}
# Words that genuinely signal WEIGHT. Deliberately excludes "solid"/"massiv"/
# "massiccio"/"macizo"/"massief" (those mean "not plated/hollow", not heavy --
# a live-run audit showed "9ct Solid Gold" ladies' signets being estimated at
# 12g+) and "large"/"big" (finger size, handled by the size bump instead).
HEAVY_WORDS = ("heavy", "chunky", "substantial", "massive", "thick",
               "heavyweight", "heavy gauge", "schwer", "wuchtig")
# NB: no "fine" here -- "fine gold" means pure gold, not a slim ring.
SLIM_WORDS = ("dainty", "slim", "thin", "narrow", "petite", "slender",
              "delicate")

# --- Stone handling (see assess_ring) ------------------------------------
# Stone "showcases" where the gold is a minor fraction -> reject outright.
STONE_SHOWCASE_WORDS = ("cluster", "halo", "trilogy", "three stone", "3 stone",
                        "five stone", "5 stone", "eternity", "dress ring",
                        "cocktail", "multi gem", "multi-gem", "gem set",
                        "gemset", "gemstone cluster", "diamond cluster")
# Two or more DIFFERENT gem families named (e.g. "Diamanten grüne Turmaline")
# also means a stone showcase -- the gold is the mount, not the mass.
SHOWCASE_MIN_DISTINCT_STONES = 2
# Carved-stone / hardstone signets where gold still dominates -> keep, but
# subtract a stone-mass allowance before testing the gold floor.
INTAGLIO_WORDS = ("intaglio", "carved", "seal", "hardstone", "bloodstone",
                  "carnelian", "cornelian", "agate", "onyx", "lapis", "jasper")
STONE_ALLOWANCE_G = 1.5        # generic single small stone in a signet/band
INTAGLIO_ALLOWANCE_G = 3.5     # carved hardstone seal face (bigger displacement)

# --- Style / era tags (search + card tags) -------------------------------
STYLE_TAG_WORDS = {
    "signet":   ("signet", "siegelring", "chevalière", "chevalier", "sello",
                 "sigillo", "zegelring", "sygnet"),
    "intaglio": ("intaglio", "intaille", "carved", "seal", "hardstone",
                 "bloodstone", "carnelian", "agate"),
    "vintage":  ("vintage", "retro", "mid century", "mid-century"),
    "antique":  ("antique", "georgian", "victorian", "edwardian", "deco",
                 "antik", "antico", "antiguo"),
    "band":     ("band", "wedding ring", "wedding band", "ehering", "fede"),
    "gents":    ("gents", "gent's", "men's", "mens", "herren", "homme", "uomo",
                 "hombre", "heren"),
}

# --- Seller type ----------------------------------------------------------
# eBay marks registered traders as BUSINESS sellers (EU/UK consumer law), so
# sellerAccountType=INDIVIDUAL is a private seller -- where mispriced rings
# live; dealers price at retail. "Individual" accounts with big feedback are
# shops in practice, so the strict "private" flag also requires modest
# feedback and no Top Rated badge.
PRIVATE_FEEDBACK_MAX = 1000


def classify_seller(seller, top_rated=False):
    """Return (seller_type, feedback_score, is_private) from an API seller obj.

    seller_type: 'private' | 'business' | None (field absent).
    is_private:  strict flag -- INDIVIDUAL account, feedback below
                 PRIVATE_FEEDBACK_MAX, and not Top Rated.
    """
    seller = seller or {}
    acct = (seller.get("sellerAccountType") or "").upper()
    try:
        fb = int(seller.get("feedbackScore") or 0)
    except (TypeError, ValueError):
        fb = 0
    stype = {"INDIVIDUAL": "private", "BUSINESS": "business"}.get(acct)
    is_private = stype == "private" and fb < PRIVATE_FEEDBACK_MAX and not top_rated
    return stype, fb, is_private


# --- Detail fetching ------------------------------------------------------
# The search endpoint only returns a short description. If the weight isn't
# found there, optionally call the getItem endpoint for the FULL description.
# This costs one extra API call per weight-unknown item (still bounded by
# MAX_ITEMS), so it's worth it but can be disabled for speed.
FETCH_FULL_DETAILS = True
# Cap detail look-ups PER carat scan as a safety bound on runtime / eBay rate
# limits. Items beyond the cap keep "weight unknown". 0 = no cap.
# Quota math: 4 carats x (~130 search + 250 item) ~ 1,520 calls/day, well
# inside eBay's default 5,000/day Browse quota. Each successful look-up turns
# an unknown into a confirmed weight (usable value flag) or a sub-15g drop --
# both better than review-lane noise.
MAX_DETAIL_FETCHES = 250

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
    # Mixed silver/gold pieces in other languages -- the weight is mostly
    # silver, so melt on the stated weight is fiction ("Anello Argento 18k").
    "argento", "argent ", "silber", "zilver", "plata ", "925",
    # Gold-fronted/plated base metals -- weight is mostly the base metal.
    "gold on brass", "gold and brass", "gold & brass", "on brass", "brass",
    "gold on copper", "base metal", "pinchbeck",
]

# Listings that ARE gold but aren't the rings we want (coin-mounted pieces:
# the weight includes a coin/mount and they carry a numismatic premium, so the
# melt figure is misleading). Edit this list to taste.
EXCLUDE_MARKERS = [
    "sovereign", "half sov", "full sov", " sov ", "krugerrand",
    "coin", "guinea", "ducat",
    # Multi-ring bundles: the stated weight is the whole lot, not one signet.
    # (Bare "lot" is too common in genuine titles to exclude.)
    "bundle", "job lot", "joblot", "lot of ", "x rings", "rings x",
]


def is_excluded(text):
    """True if the listing is an unwanted type (e.g. coin/sovereign rings)."""
    if not text:
        return False
    low = f" {text.lower()} "
    return any(marker in low for marker in EXCLUDE_MARKERS)

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

# Number (comma or dot decimal) immediately followed by a gram unit, in several
# languages. Handles: 9.4g  9.4 g  9.4grams  9,4 g  21 grammi  18,4 Gramm  9.4gr
_WEIGHT_RE = re.compile(
    r"(\d{1,3}(?:[.,]\d{1,2})?)\s*"
    r"(?:grammes?|grammi|gramm|grams?|gms?|gm|grm?s?|gr|g)\b\.?",
    re.IGNORECASE,
)

# Carat patterns -> carat number. Checked in order; first hit wins per group.
_CARAT_PATTERNS = [
    (24, re.compile(r"\b24\s*c?t\b|\b24\s*k(?:t)?\b|\b24\s*carat\b|\b999\b", re.I)),
    (22, re.compile(r"\b22\s*c?t\b|\b22\s*k(?:t)?\b|\b22\s*carat\b|\b916\b|\b917\b", re.I)),
    (20, re.compile(r"\b20\s*c?t\b|\b20\s*k(?:t)?\b|\b20\s*carat\b|\b833\b", re.I)),
    (18, re.compile(r"\b18\s*c?t\b|\b18\s*k(?:t)?\b|\b18\s*carat\b|\b750\b", re.I)),
    (15, re.compile(r"\b15\s*c?t\b|\b15\s*k(?:t)?\b|\b15\s*carat\b|\b625\b", re.I)),
    (14, re.compile(r"\b14\s*c?t\b|\b14\s*k(?:t)?\b|\b14\s*carat\b|\b585\b", re.I)),
    (10, re.compile(r"\b10\s*c?t\b|\b10\s*k(?:t)?\b|\b10\s*carat\b|\b417\b", re.I)),
    (9,  re.compile(r"\b9\s*c?t\b|\b9\s*k(?:t)?\b|\b9\s*carat\b|\b375\b", re.I)),
    (8,  re.compile(r"\b8\s*c?t\b|\b8\s*k(?:t)?\b|\b8\s*carat\b|\b333\b", re.I)),
]


# A number immediately after one of these is a RING SIZE, not a weight --
# "misura 15 g. 6,20" is Italian for "size 15, 6.20 grams".
_SIZE_WORDS = ("size", "sz", "misura", "taglia", "größe", "grösse", "gr.",
               "talla", "maat", "pointure", "rozmiar")

# European unit-first style: "g. 6,20" / "grammi 12,5" (unit BEFORE number).
# Deliberately excludes "gr" -- German "Gr. 60" means SIZE 60, not 60 grams.
# The bare "g." form additionally requires a decimal for the same reason.
_WEIGHT_UNIT_FIRST_RE = re.compile(
    r"\b(?:(?:grammes?|grammi|gramm|grams?)\.?\s*(\d{1,2}(?:[.,]\d{1,2})?)"
    r"|g\.\s*(\d{1,2}[.,]\d{1,2}))\b", re.I)


def parse_weight_grams(text):
    """Return the most plausible gold weight in grams, or None if not found.

    Picks the LARGEST matched gram figure on the assumption that it's the total
    item weight (sellers sometimes also quote tiny stone weights). Numbers that
    follow a ring-size word are skipped ("misura 15 g. 6,20" -> 6.2, not 15).
    """
    if not text:
        return None
    candidates = []
    for m in _WEIGHT_RE.finditer(text):
        # Skip if the number is actually a ring size ("size 15 g..." trap).
        lead = text[max(0, m.start() - 12):m.start()].lower()
        if any(w in lead for w in _SIZE_WORDS):
            continue
        raw = m.group(1).replace(",", ".")
        try:
            grams = float(raw)
        except ValueError:
            continue
        # Sanity bounds: even a heavy signet is realistically under ~80g;
        # bigger figures are almost always a misparse (packaging, dimensions).
        if 0.3 <= grams <= 80:
            candidates.append(grams)
    for m in _WEIGHT_UNIT_FIRST_RE.finditer(text):
        # Unit-first only counts when the unit does NOT already belong to a
        # preceding number: in "4,5 Gramm 61" the 61 is a RING SIZE, and the
        # unit belongs to 4,5 (already captured by the number-first pass).
        lead = text[max(0, m.start() - 4):m.start()]
        if re.search(r"\d", lead):
            continue
        raw = (m.group(1) or m.group(2)).replace(",", ".")
        try:
            grams = float(raw)
        except ValueError:
            continue
        if 0.3 <= grams <= 80:
            candidates.append(grams)
    if not candidates:
        return None
    return max(candidates)


# Gemstone words. A carat figure directly followed by one of these is a STONE
# weight (e.g. "22ct smoky quartz"), not the gold's carat -- don't read it as gold.
STONE_WORDS = [
    "diamond", "sapphire", "emerald", "ruby", "quartz", "topaz", "amethyst",
    "garnet", "opal", "zirconia", "cz", "citrine", "peridot", "spinel",
    "tanzanite", "aquamarine", "cubic", "moissanite", "onyx", "turquoise",
    "pearl", "stone", "gemstone", "crystal", "paste", "tourmaline", "jade",
    "morganite", "kunzite",
    # German (a 56g "Turmaline" statement ring was melt-valued as all-gold)
    "diamant", "diamanten", "brillant", "saphir", "smaragd", "rubin",
    "turmalin", "granat", "zirkonia", "perle", "edelstein",
    # French
    "diamants", "émeraude", "emeraude", "rubis", "améthyste", "amethyste",
    "grenat", "topaze", "perle", "pierre",
    # Italian ("Ametista" slipped through as all-gold weight)
    "diamante", "diamanti", "zaffiro", "smeraldo", "rubino", "ametista",
    "granato", "topazio", "citrino", "perla", "pietra", "acquamarina",
    # Spanish
    "diamante", "zafiro", "esmeralda", "rubí", "rubi", "amatista",
    "granate", "topacio", "perla", "piedra",
    # Dutch
    "diamant", "saffier", "smaragd", "robijn", "amethist", "granaat",
    "parel", "edelsteen",
]

# Gold fineness hallmark numbers -> carat (unambiguous; preferred over "Nct").
# Includes continental marks: 833 = 20ct, 333 = 8ct (common in DE/NL).
_HALLMARK = [("999", 24), ("990", 24), ("917", 22), ("916", 22), ("833", 20),
             ("750", 18), ("625", 15), ("585", 14), ("417", 10), ("375", 9),
             ("333", 8)]


def has_stones(text):
    """True if the listing mentions a gemstone (so weight isn't all gold)."""
    if not text:
        return False
    low = text.lower()
    return any(re.search(r"\b" + s + r"\b", low) for s in STONE_WORDS)


# Gem FAMILIES across languages, for counting how many different gems a
# listing names. Prefix-matched so plurals/inflections count ("Diamanten").
_STONE_FAMILIES = {
    "diamond":    ("diamond", "diamant", "brillant"),
    "sapphire":   ("sapphire", "saphir", "zaffir", "zafiro", "saffier"),
    "emerald":    ("emerald", "émeraude", "emeraude", "smaragd", "smeraldo",
                   "esmeralda"),
    "ruby":       ("ruby", "rubis", "rubin", "robijn"),
    "amethyst":   ("amethyst", "améthyste", "ametista", "amatista", "amethist"),
    "tourmaline": ("tourmaline", "turmalin"),
    "garnet":     ("garnet", "grenat", "granat", "granato", "granaat"),
    "topaz":      ("topaz", "topaze", "topazio", "topacio"),
    "citrine":    ("citrine", "citrino"),
    "opal":       ("opal",),
    "aquamarine": ("aquamarine", "acquamarina", "aigue-marine"),
    "pearl":      ("pearl", "perle", "perla", "parel"),
    "quartz":     ("quartz",),
}


def distinct_stone_families(text):
    """How many DIFFERENT gem families the listing names (any language)."""
    low = (text or "").lower()
    return sum(1 for words in _STONE_FAMILIES.values()
               if any(re.search(r"\b" + w, low) for w in words))


def detect_carat(text):
    """Return (carat, assumed_flag). assumed_flag=True when we fell back to default.

    Prefers hallmark fineness numbers, then "<n>ct/k/carat" -- but ignores a
    carat figure immediately followed by a gemstone word (that's a stone weight,
    e.g. "22ct smoky quartz", not 22ct gold).
    """
    if not text:
        return DEFAULT_CARAT, True
    low = text.lower()
    for num, carat in _HALLMARK:
        if re.search(r"\b" + num + r"\b", low):
            return carat, False
    for carat, pattern in _CARAT_PATTERNS:
        for m in pattern.finditer(low):
            tail = low[m.end():m.end() + 16]
            if any(s in tail for s in STONE_WORDS):
                continue   # "<n>ct <stone>" -> stone weight, not gold carat
            return carat, False
    return DEFAULT_CARAT, True


def style_tags(text):
    """Return the list of style/era tags present in the text (signet, vintage...)."""
    low = (text or "").lower()
    return [tag for tag, words in STYLE_TAG_WORDS.items()
            if any(w in low for w in words)]


def estimate_weight_low(text, carat):
    """Conservative LOWER-BOUND gram estimate when no weight is stated.

    Estimates ONLY when the seller gives an explicit weight adjective (heavy/
    chunky/... or slim/dainty/...) alongside a ring archetype. A plain
    "9ct gold signet ring" carries no weight information at all, so it returns
    None (-> UNKNOWN, routed to the review lane) rather than inventing a figure.
    A live-run audit showed archetype-only guesses polluting the value list.
    """
    low = (text or "").lower()
    heavy = any(w in low for w in HEAVY_WORDS)
    slim = any(w in low for w in SLIM_WORDS)
    if not heavy and not slim:
        return None   # no explicit weight signal -> unknown / review lane

    is_signet = any(w in low for w in STYLE_TAG_WORDS["signet"])
    is_band = any(w in low for w in STYLE_TAG_WORDS["band"])
    if is_signet:
        arch = "signet_heavy" if heavy else "signet"
    elif is_band:
        arch = "band_heavy" if heavy else "band"
    else:
        arch = "ring_heavy" if heavy else "band"   # slim generic ring ~ band

    base = ARCHETYPE_LOW_G[arch]
    if slim:
        base *= 0.7
    # Large US ring size (>= ~11 / letter T+) means more metal: nudge up.
    msize = re.search(r"\bsize\s*([0-9]{1,2}(?:\.5)?)\b", low)
    if msize and float(msize.group(1)) >= 11:
        base *= 1.15
    est = base * RING_DENSITY.get(carat, _DENSITY_BASE) / _DENSITY_BASE
    return round(est, 1)


def assess_ring(text, carat, stated_weight):
    """Decide gold weight, confidence, and keep/reject for one listing.

    Returns a dict:
      keep            -- bool (False = reject, e.g. gold-light stone showcase)
      confidence      -- 'confirmed' | 'estimated' | 'unknown'
      gross_g         -- parsed/estimated total weight (or None)
      net_gold_g      -- gold weight after subtracting a stone allowance (or None)
      stones          -- bool
      tags            -- style/era tags
    """
    low = (text or "").lower()
    tags = style_tags(text)

    # Gold-light showcases: the gold is a minor fraction -> reject. Either an
    # explicit showcase word, or >= 2 different gem families named (the gold
    # is a mount for the stones, so weight-based melt is meaningless).
    # Brand mode skips this: no melt is computed, and stone-set Yurman
    # signets are legitimate brand pieces.
    if (METAL != "yurman"
            and (any(w in low for w in STONE_SHOWCASE_WORDS)
                 or distinct_stone_families(text) >= SHOWCASE_MIN_DISTINCT_STONES)):
        return {"keep": False, "confidence": "reject", "gross_g": None,
                "net_gold_g": None, "stones": True, "tags": tags}

    # Off-target style: we only want signets/intaglios (see REQUIRED_TAGS).
    if REQUIRED_TAGS and not (set(tags) & set(REQUIRED_TAGS)):
        return {"keep": False, "confidence": "off-target", "gross_g": None,
                "net_gold_g": None, "stones": False, "tags": tags}

    stones = has_stones(text)
    intaglio = any(w in low for w in INTAGLIO_WORDS)
    allowance = (INTAGLIO_ALLOWANCE_G if intaglio
                 else STONE_ALLOWANCE_G if stones else 0.0)

    if stated_weight is not None:
        confidence = "confirmed"
        gross = stated_weight
    else:
        est = estimate_weight_low(text, carat)
        if est is not None:
            confidence, gross = "estimated", est
        else:
            confidence, gross = "unknown", None

    net = round(gross - allowance, 1) if gross is not None else None

    # Weight-window test against NET gold weight. In strict mode only a
    # seller-stated weight inside [floor, ceiling] survives. Brand mode
    # (yurman) has no weight requirement at all -- it's not a melt hunt.
    if METAL == "yurman":
        keep = True
    elif confidence == "confirmed":
        keep = (net is not None
                and WEIGHT_FLOOR_CONFIRMED <= net <= WEIGHT_CEILING_CONFIRMED)
    elif confidence == "estimated":
        keep = (not STRICT_CONFIRMED_ONLY and net is not None
                and net >= WEIGHT_FLOOR_ESTIMATED_LOWBOUND)
    else:  # unknown: review lane only when strict mode is off
        keep = not STRICT_CONFIRMED_ONLY

    return {"keep": keep, "confidence": confidence, "gross_g": gross,
            "net_gold_g": net, "stones": stones, "tags": tags}


# Melt/landed above this is almost always a data artefact (weight misparse,
# multi-item lot, stolen-photo scam) -- nobody knowingly sells at <40% of
# scrap. Suspects stay listed but are never flagged as a buy signal.
SUSPECT_RATIO = 2.5


def value_flag(melt, landed, confidence):
    """The core economic test: is this ring a VALUE candidate?

    Requires a CONFIRMED (seller-stated, parsed) weight. Estimated weights are
    kept and shown with an indicative ratio, but a buy signal must never rest
    on a guessed weight -- that's how fake bargains get flagged. Ratios beyond
    SUSPECT_RATIO are demoted as too-good-to-be-true.
    """
    return bool(melt and landed and confidence == "confirmed"
                and landed < melt * MELT_THRESHOLD
                and melt < landed * SUSPECT_RATIO)


def detect_silver_fineness(text):
    """Return (fraction, mark, assumed) for a silver listing.

    Prefers an explicit fineness mark (925/958/900/835/830/800); "sterling"
    means 925. Anything else assumes sterling with the assumed flag set.
    """
    low = (text or "").lower()
    for mark, frac in SILVER_FINENESS.items():
        if re.search(r"\b" + mark + r"\b", low):
            return frac, mark, False
    if "sterling" in low or "925" in low:
        return 0.925, "925", False
    return 0.925, "925", True


def is_silver_excluded(text):
    """Silver-mode reject: plated/costume/base-metal, or actually a GOLD ring."""
    low = (text or "").lower()
    if any(m in low for m in SILVER_PLATED_MARKERS):
        return True
    carat, assumed = detect_carat(low)
    return not assumed          # an explicit gold carat/fineness mark -> gold ring


def is_yurman_excluded(text):
    """Yurman-mode reject: not claiming the brand, or a lookalike/replica."""
    low = (text or "").lower()
    if "yurman" not in low:
        return True                 # search noise -- brand not even claimed
    return any(m in low for m in YURMAN_FAKE_MARKERS)


def is_plated(text):
    """True if the text strongly suggests plated / rolled / non-solid gold."""
    if not text:
        return False
    low = f" {text.lower()} "
    return any(marker in low for marker in PLATED_MARKERS)

# =============================================================================
# Gold spot price
# =============================================================================

def _free_spot_gbp_per_oz(symbol="XAU"):
    """Keyless live price: gold-api.com (USD/oz) x Frankfurter ECB (USD->GBP).

    Both are free and need no API key, so this works in CI with zero secrets.
    symbol: XAU for gold, XAG for silver.
    Returns (price_gbp_per_oz, label) or raises on failure.
    """
    g = requests.get(f"https://api.gold-api.com/price/{symbol}", timeout=30)
    g.raise_for_status()
    usd_per_oz = float(g.json()["price"])
    fx = requests.get("https://api.frankfurter.dev/v1/latest",
                      params={"base": "USD", "symbols": "GBP"}, timeout=30)
    fx.raise_for_status()
    usd_to_gbp = float(fx.json()["rates"]["GBP"])
    return usd_per_oz * usd_to_gbp, f"gold-api.com x frankfurter (live, ${usd_per_oz:,.2f}/oz)"


def get_silver_spot_gbp_per_oz():
    """Return (silver_price_per_troy_oz_GBP, source_label)."""
    try:
        return _free_spot_gbp_per_oz("XAG")
    except (requests.RequestException, KeyError, ValueError, TypeError) as e:
        print(f"[warn] silver price lookup failed ({e}); using fallback.",
              file=sys.stderr)
        return SPOT_SILVER_GBP_PER_OZ, "hardcoded fallback"


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


def get_fx_to_gbp():
    """Return {currency: multiplier to convert that currency into GBP}.

    e.g. {"USD": 0.79, "EUR": 0.85, "GBP": 1.0}. Falls back to constants if the
    live FX lookup fails.
    """
    fx = {"GBP": 1.0}
    # Every non-GBP currency used by a configured marketplace.
    symbols = ",".join(sorted({c["currency"] for c in MARKETPLACES.values()
                               if c["currency"] != "GBP"}))
    try:
        r = requests.get("https://api.frankfurter.dev/v1/latest",
                         params={"base": "GBP", "symbols": symbols}, timeout=30)
        r.raise_for_status()
        for cur, gbp_to_cur in r.json()["rates"].items():
            fx[cur] = 1.0 / float(gbp_to_cur)   # invert GBP->cur to get cur->GBP
        return fx
    except (requests.RequestException, KeyError, ValueError, TypeError, ZeroDivisionError) as e:
        print(f"[warn] FX lookup failed ({e}); using fallback rates.", file=sys.stderr)
        return {cur: 1.0 / per for cur, per in FX_FALLBACK_PER_GBP.items()}

# =============================================================================
# eBay Browse API: search + item detail
# =============================================================================

_BUYING_FILTER = {
    "auction": "AUCTION",
    "fixed": "FIXED_PRICE",
    "both": "AUCTION|FIXED_PRICE",
}


API_CALLS = {"search": 0, "item": 0}      # quota-spend instrumentation


def _get_with_retry(url, headers, params, label, retries=4):
    """GET with exponential backoff on eBay 429 / transient 400 / 5xx.

    eBay's Browse API intermittently returns HTTP 400 for a perfectly valid
    request under load (verified: the identical request succeeds on re-send).
    A real bad request just wastes the 2 extra attempts, so retrying is cheap
    insurance against losing a whole market/query slice from one blip.
    """
    r = None
    kind = "item" if "/item/" in url else "search"
    for attempt in range(retries):
        API_CALLS[kind] += 1
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code == 200:
            return r
        retryable = r.status_code == 429 or r.status_code >= 500 \
            or (r.status_code == 400 and attempt < 2)
        if not retryable:
            return r
        wait = 2 ** attempt          # 1, 2, 4, 8s
        print(f"[warn] {r.status_code} on {label}; backing off {wait}s "
              f"({r.text[:120]})", file=sys.stderr)
        time.sleep(wait)
    return r


def search_listings(token, queries, max_price_gbp, max_items, buying="both",
                    market="EBAY_GB", fx=None, min_price_gbp=0, conditions="USED"):
    """Search one or more queries on a single marketplace; merge & de-duplicate.

    `max_price_gbp` is converted to the marketplace's own currency for the API
    price filter. Each returned item is tagged with its marketplace metadata
    (_market_id / _currency / _country / _flag) for later GBP conversion.
    """
    cfg = MARKETPLACES[market]
    currency = cfg["currency"]
    fx = fx or {currency: 1.0}
    # Convert the £ price bounds into this marketplace's currency for the filter.
    per_gbp = (1.0 / fx[currency]) if fx.get(currency) else 1.0   # GBP -> currency
    max_price = round(max_price_gbp * per_gbp)
    min_price = round(min_price_gbp * per_gbp)

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": market,
        "Content-Type": "application/json",
    }
    buy = _BUYING_FILTER.get(buying, _BUYING_FILTER["both"])
    # Browse API filter syntax. priceCurrency is required alongside a price range.
    parts = [f"buyingOptions:{{{buy}}}"]
    if conditions and conditions.upper() != "ANY":
        parts.append("conditions:{USED}")
    parts.append(f"price:[{min_price}..{max_price}]")
    parts.append(f"priceCurrency:{currency}")
    item_filter = ",".join(parts)

    seen = set()
    items = []
    # eBay requires `offset` to be a MULTIPLE of `limit` (error 12515), so the
    # page size must stay CONSTANT for the whole of a query's pagination. A
    # shrinking page (limit = remaining budget) 400s every request after the
    # first and silently loses that market/query slice.
    page = min(PAGE_SIZE, max_items)
    for query in queries:
        offset = 0
        while len(items) < max_items:
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
            r = _get_with_retry(SEARCH_URL, headers, params, f"{market}/{query}")
            if r.status_code != 200:
                print(f"[warn] search '{query}' ({market}) failed ({r.status_code})",
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
                it["_market_id"] = market
                it["_currency"] = currency
                it["_country"] = cfg["country"]
                it["_flag"] = cfg["flag"]
                items.append(it)
            total = data.get("total", 0)
            offset += page
            if not batch or offset >= total or len(items) >= max_items:
                break
            time.sleep(0.2)  # be polite between pages
        if len(items) >= max_items:
            break

    return items[:max_items]


def fetch_item_description(token, item_id, market="EBAY_GB"):
    """Return the full description text for an item, or '' on failure."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": market,
    }
    try:
        r = _get_with_retry(ITEM_URL.format(item_id=item_id), headers, None,
                            f"{market}/item")
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

_CURRENCY_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€"}


def analyse(items, token, spot_per_oz, fx=None):
    """Turn raw eBay summaries into analysed ring records (prices in GBP)."""
    spot_per_gram_fine = spot_per_oz / TROY_OZ_IN_GRAMS
    fx = fx or {}
    records = []
    detail_fetches = 0   # bound getItem calls per scan (see MAX_DETAIL_FETCHES)

    for item in items:
        title = item.get("title", "")
        short_desc = item.get("shortDescription", "") or ""
        text = f"{title} {short_desc}"
        market = item.get("_market_id", "EBAY_GB")
        currency = item.get("_currency", "GBP")

        # 1. Reject wrong-metal / plated / lookalike and unwanted types.
        if METAL == "yurman":
            if is_yurman_excluded(text) or is_excluded(text):
                continue
        elif METAL == "silver":
            if is_silver_excluded(text) or is_excluded(text):
                continue
        elif is_plated(text) or is_excluded(text):
            continue

        # 2. Carat/fineness + weight from summary text. The TITLE weight wins
        #    over any description figure -- a live audit caught a desc number
        #    (20.1) overriding the seller's stated title weight (10,20g).
        if METAL == "silver":
            fineness_frac, fineness_mark, carat_assumed = detect_silver_fineness(text)
            carat = None
        elif METAL == "yurman":
            fineness_frac, fineness_mark, carat_assumed = None, None, False
            carat = None
        else:
            fineness_frac, fineness_mark = None, None
            carat, carat_assumed = detect_carat(text)
        weight = parse_weight_grams(title)
        if weight is None:
            weight = parse_weight_grams(short_desc)

        # Multi-variant listings: the API price is the CHEAPEST variant and any
        # stated weight is some variant's -- the melt comparison is meaningless.
        # Route to the review lane and don't spend a detail fetch on it.
        is_variant = item.get("itemGroupType") == "SELLER_DEFINED_VARIATIONS"
        if is_variant:
            weight = None

        # 3. If weight still unknown, dig into the full description (capped per
        #    scan). Only worth a call for on-target styles -- anything without
        #    a signet/intaglio word is dropped later regardless.
        on_target = not REQUIRED_TAGS or bool(set(style_tags(text)) & set(REQUIRED_TAGS))
        if (weight is None and not is_variant and on_target and FETCH_FULL_DETAILS
                and METAL != "yurman"   # brand mode needs no weight -> save quota
                and (MAX_DETAIL_FETCHES == 0 or detail_fetches < MAX_DETAIL_FETCHES)):
            detail_fetches += 1
            full = fetch_item_description(token, item.get("itemId", ""), market)
            if full:
                bad = (is_silver_excluded(full) if METAL == "silver"
                       else is_plated(full)) or is_excluded(full)
                if bad:
                    continue
                text = f"{text} {full}"
                weight = parse_weight_grams(full)
                if carat_assumed and METAL == "silver":
                    fineness_frac, fineness_mark, carat_assumed = \
                        detect_silver_fineness(full)
                elif carat_assumed:
                    carat, carat_assumed = detect_carat(full)
            time.sleep(0.1)  # polite pacing for the extra call

        # 4. Weight-confidence + net-gold assessment (handles stones, archetype
        #    estimation, and the 15g/12g floor branching).
        a = assess_ring(text, carat, weight)
        if not a["keep"]:
            continue
        if is_variant and a["confidence"] == "estimated":
            # No weight claim can be trusted on a variant listing.
            a.update(confidence="unknown", gross_g=None, net_gold_g=None)
        confidence = a["confidence"]      # confirmed | estimated | unknown
        net_gold = a["net_gold_g"]        # gold weight used for melt (or None)

        raw_bid = get_current_bid(item)              # in listing currency
        rate = fx.get(currency, 1.0)                 # currency -> GBP
        bid = round(raw_bid * rate, 2) if raw_bid is not None else None
        price_orig = None
        if raw_bid is not None and currency != "GBP":
            price_orig = f"{_CURRENCY_SYMBOL.get(currency, '')}{raw_bid:,.0f}"

        country = item.get("_country", "UK")
        # True £ cost to a UK buyer (adds import VAT + postage for non-UK).
        landed = landed_cost(bid, country)

        if METAL == "yurman":
            # Brand hunt: melt on mixed-metal branded pieces is fiction.
            melt, ratio, is_value = None, None, False
        else:
            if METAL == "silver":
                fraction = fineness_frac
            else:
                fraction = CARAT_FRACTION.get(carat, CARAT_FRACTION[DEFAULT_CARAT])
            content_per_gram = spot_per_gram_fine * fraction
            melt = round(net_gold * content_per_gram, 2) if net_gold else None
            ratio = round(melt / landed, 2) if (melt and landed) else None
            # VALUE flag: landed cost below melt * threshold, CONFIRMED weight
            # only. Net-gold already excludes stone mass, so stone-set signets
            # can legitimately qualify. Estimated/unknown never flag value.
            is_value = value_flag(melt, landed, confidence)

        stype, fb, is_priv = classify_seller(
            item.get("seller"), item.get("topRatedBuyingExperience", False))

        records.append({
            "title": title,
            "metal": METAL,                 # gold | silver
            "carat": carat,                 # gold only (None for silver)
            "fineness": fineness_mark,      # silver only ("925", "800", ...)
            "carat_assumed": carat_assumed,
            "seller_type": stype,           # private | business | None
            "seller_feedback": fb,
            "seller_private": is_priv,      # strict: individual + modest fb
            "weight_g": a["gross_g"],        # displayed weight (gross)
            "net_gold_g": net_gold,          # gold-only weight used for melt
            "weight_confidence": confidence, # confirmed | estimated | unknown
            "tags": a["tags"],               # style/era tags
            "stones": a["stones"],
            "current_bid": bid,              # GBP (item price/bid)
            "landed_cost": landed,           # GBP incl. import VAT + postage
            "price_orig": price_orig,        # original currency (non-UK only)
            "country": country,
            "flag": item.get("_flag", "🇬🇧"),
            "melt_value": melt,
            "ratio": ratio,                  # melt / landed cost
            "is_value": is_value,
            "buying": buying_type(item),
            "condition": item.get("condition"),   # "Pre-owned" / "New ..." etc
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


def mark_new_arrivals(records, path, now_iso):
    """Stamp each record with first_seen, and flag genuinely NEW arrivals.

    Compares against the previous scan file at `path` (keyed by listing URL):
      - already there  -> carry its first_seen forward, is_new = False
      - not there      -> first_seen = this run, is_new = True
    A listing missing from the previous file because that scan was empty or
    the file didn't exist yet is NOT treated as a false "new" flood: with no
    previous data at all we stamp first_seen but leave is_new False, so the
    first run just establishes the baseline.
    """
    prev = {}
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        for r in old.get("results", []):
            if r.get("url"):
                prev[r["url"]] = r.get("first_seen")
    except (OSError, json.JSONDecodeError, KeyError):
        prev = {}

    baseline = not prev          # nothing to compare against -> no NEW badges
    fresh = 0
    for r in records:
        url = r.get("url")
        if url in prev:
            r["first_seen"] = prev[url] or now_iso
            r["is_new"] = False
        else:
            r["first_seen"] = now_iso
            r["is_new"] = not baseline
            if r["is_new"]:
                fresh += 1
    if baseline:
        print(f"  new arrivals: baseline scan (no previous data to compare)")
    else:
        print(f"  new arrivals: {fresh} of {len(records)} not in the previous scan")
    return records


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
    fields = ["title", "carat", "weight_g", "net_gold_g", "weight_confidence",
              "tags", "stones", "is_new", "first_seen",
              "seller_type", "seller_feedback", "seller_private",
              "current_bid", "landed_cost", "price_orig",
              "country", "melt_value", "ratio", "is_value", "buying", "bids",
              "time_left", "url"]
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
    p.add_argument("--markets", default=DEFAULT_MARKETS,
                   help="comma-separated eBay marketplaces to scan, e.g. "
                        "EBAY_GB,EBAY_US,EBAY_DE (prices converted to GBP)")
    p.add_argument("--min-price", type=float, default=0.0,
                   help="min current price (GBP) for the API filter; a coarse "
                        "weight proxy that skips tiny rings")
    p.add_argument("--min-weight", type=float, default=0.0,
                   help="drop rings under this many grams (and weight-unknown) "
                        "from the output")
    p.add_argument("--conditions", default="USED", choices=["USED", "ANY"],
                   help="USED only, or ANY condition (captures dealer 'New')")
    p.add_argument("--metal", default="gold", choices=["gold", "silver", "yurman"],
                   help="gold (default), silver (sterling signet mode with live "
                        "silver spot), or yurman (David Yurman brand hunt: no "
                        "weight floor, no melt -- genuine-brand claims only)")
    return p.parse_args()


def main():
    global MELT_THRESHOLD, DEFAULT_CARAT, WEIGHT_FLOOR_CONFIRMED, METAL
    args = parse_args()
    METAL = args.metal
    MELT_THRESHOLD = args.threshold
    if METAL == "silver" and args.threshold == 1.3:
        # Silver trades further above melt; use the silver default unless the
        # user explicitly set a threshold.
        MELT_THRESHOLD = SILVER_MELT_THRESHOLD
    DEFAULT_CARAT = args.default_carat
    if args.min_weight > 0:                      # --min-weight tunes the hard floor
        WEIGHT_FLOOR_CONFIRMED = args.min_weight
    elif METAL == "silver":
        WEIGHT_FLOOR_CONFIRMED = SILVER_WEIGHT_FLOOR

    extra = [q.strip() for q in args.query.split("||") if q.strip()] \
        if args.query.strip() != SEARCH_TERM else []
    markets = [m.strip() for m in args.markets.split(",")
               if m.strip() in MARKETPLACES]
    if not markets:
        markets = ["EBAY_GB"]

    print(f"\neBay {METAL.title()} Ring Scanner")
    print(f"  {f'metal={METAL}' if METAL != 'gold' else f'carat={args.default_carat}'}  "
          f"buying={args.buying}  conditions={args.conditions}  "
          f"price=£{args.min_price:.0f}-£{args.max_price:.0f}  "
          f"floors: confirmed>={WEIGHT_FLOOR_CONFIRMED}g estimated>={WEIGHT_FLOOR_ESTIMATED_LOWBOUND}g  "
          f"limit={args.limit}/mkt  markets={len(markets)}  threshold={MELT_THRESHOLD}")

    if METAL == "yurman":
        spot, source = 0.0, "n/a (brand mode -- no melt)"
        print("  brand mode: David Yurman -- no melt valuation")
    elif METAL == "silver":
        spot, source = get_silver_spot_gbp_per_oz()
        print(f"  silver spot: £{spot:,.2f}/oz  (source: {source})")
    else:
        spot, source = get_spot_price_gbp_per_oz()
        print(f"  gold spot: £{spot:,.2f}/oz  (source: {source})")
    fx = get_fx_to_gbp()
    print(f"  fx to GBP: " + ", ".join(f"{c}={v:.3f}" for c, v in sorted(fx.items()) if c != "GBP"))

    token = get_ebay_token()
    items, seen = [], set()
    raw_by_country = {}
    all_queries = []                 # union of every per-market query (for meta)
    for mkt in markets:
        cfg = MARKETPLACES[mkt]
        mkt_queries = queries_for(mkt, args.default_carat, extra=extra, metal=METAL)
        for q in mkt_queries:
            if q not in all_queries:
                all_queries.append(q)
        got = search_listings(token, mkt_queries, args.max_price, args.limit,
                              args.buying, market=mkt, fx=fx,
                              min_price_gbp=args.min_price, conditions=args.conditions)
        fresh = [it for it in got if it.get("itemId") not in seen]
        seen.update(it.get("itemId") for it in got)
        items.extend(fresh)
        raw_by_country[cfg["country"]] = len(fresh)
        print(f"  {mkt}: {len(mkt_queries)}q -> {len(fresh)} new listing(s)")
        time.sleep(1)   # pace between marketplaces to ease rate limits
    print(f"  fetched {len(items)} unique listing(s) across {len(markets)} market(s).")

    records = analyse(items, token, spot, fx)
    print_table(records)

    # Per-market yield (raw fetched -> kept after filters -> flagged value) so
    # coverage leaks are visible in the run log.
    kept_by, value_by = {}, {}
    conf = {"confirmed": 0, "estimated": 0, "unknown": 0}
    for r in records:
        kept_by[r["country"]] = kept_by.get(r["country"], 0) + 1
        if r["is_value"]:
            value_by[r["country"]] = value_by.get(r["country"], 0) + 1
        conf[r["weight_confidence"]] = conf.get(r["weight_confidence"], 0) + 1
    print("  per-market yield  raw -> kept -> value:")
    for c in sorted(raw_by_country):
        print(f"    {c:3} {raw_by_country[c]:4} -> {kept_by.get(c, 0):3} -> {value_by.get(c, 0):2}")
    print(f"  weight confidence: confirmed={conf['confirmed']} "
          f"estimated={conf['estimated']} unknown={conf['unknown']}")
    print(f"  eBay API calls: search={API_CALLS['search']} item={API_CALLS['item']}")

    # Safety: if we fetched nothing at all (rate-limited / API error), keep the
    # last good scan rather than overwriting it with an empty file.
    if not items and os.path.exists(args.json):
        print(f"[warn] fetched 0 listings (likely rate-limited) -- keeping "
              f"existing {args.json}; not overwriting.", file=sys.stderr)
        return

    # Flag rings that weren't in the previous scan, so the dashboard can pin
    # today's new arrivals to the top.
    now_iso = datetime.now(timezone.utc).isoformat()
    records = mark_new_arrivals(records, args.json, now_iso)

    write_csv(records, args.csv)

    meta = {
        "generated_at": now_iso,
        "query": all_queries[0] if all_queries else args.query,
        "queries": all_queries,
        "buying": args.buying,
        "metal": METAL,
        "default_carat": args.default_carat,
        "markets": markets,
        "min_weight": args.min_weight,
        "min_price": args.min_price,
        "conditions": args.conditions,
        "max_price": args.max_price,
        "threshold": MELT_THRESHOLD,
        "spot_gbp_per_oz": spot,
        "spot_source": source,
        "carat_per_gram": {str(k): round(spot / TROY_OZ_IN_GRAMS * v, 2)
                           for k, v in CARAT_FRACTION.items()},
        "fx_to_gbp": {c: round(fx[c], 4) for c in fx},
        "total_fetched": len(items),
        "total_analysed": len(records),
        "value_count": sum(1 for r in records if r["is_value"]),
        "new_count": sum(1 for r in records if r.get("is_new")),
        "weight_unknown_count": sum(1 for r in records if r["weight_g"] is None),
        "country_counts": {c: sum(1 for r in records if r["country"] == c)
                           for c in sorted({r["country"] for r in records})},
    }
    write_json(records, args.json, meta)


if __name__ == "__main__":
    main()
