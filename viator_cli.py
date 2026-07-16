#!/usr/bin/env python3
"""
viator-cli — agent-native Viator CLI with local SQLite mirror.

NON-OBVIOUS INSIGHT
    Viator isn't just a product catalog — it's a pricing truth detector. Every
    drift between displayed HTML prices and live API prices is a signal about
    revenue leakage across the fleet.

Printing Press philosophy: local SQLite mirror beats remote API calls. Compound
commands beat ten round-trips. Structured output beats verbose logs. FTS5 on
product titles for fuzzy matching. Synced product catalog with price history.
HTML price comparison with auto-fix for clear mispricing.

COMMANDS
  viator-cli sync [--site all|porto-wine-tours] [--full]
      Sync product catalog from Viator API to local mirror. Incremental by
      default; --full forces complete reload. Writes last_product_sync_at to
      registry. Sets exit 5 on API failure.

  viator-cli products [--site porto] [--search "wine"] [--price-dropped 20]
                       [--stale] [--limit 10] [--compact] [--json] [--csv]
      Query local mirror. Zero API calls. Exit 3 if no results.
      --json: array of full product objects (all columns). Also auto-detected
              when stdout is piped. Compact+CSV have priority over json.
      --csv:  header row + data rows. Overrides --json.
      --compact: "product_code title" only. Overrides --json and --csv.
      --site: filter to products mapped to a specific site in site_products.
      --search: FTS5 full-text search on title/description. Results ranked by
                relevance (BM25) when combined with --site.

  viator-cli product 12546P1 [--prices] [--compact] [--json]
      One product's details + optional price history. Exit 3 if not found.
      Returns canonical URL with partner params. --json: {code, title, url,
      destId, rating, price}. --prices adds price_history array.
      --compact: "product_code title" only.

  viator-cli resolve "Beppu onsen tour" [--compact] [--json]
      Resolve a region+activity query to a product code via FTS5 (two-tier:
      full query first, activity-only fallback). Filters out ttd/g5335/all
      category codes. Returns canonical URL with partner params. Exit 3 if
      no match.
      --json: {"code", "title", "url", "destId", "destination_name"}.

  viator-cli prices [--site porto] [--min-pct 15] [--compact] [--json]
      Price drift report. Compares most recent API price with previous capture.
      Exit 1 if drifts found (cron-alert compatible).
      --json: {"drifts": N, "products": [...]}. Auto-detected when piped.

  viator-cli compare [--site all|porto] [--fix] [--compact] [--json]
      HTML-vs-API price comparison across fleet sites. Extracts displayed prices
      from HTML product cards, fetches live API prices from /availability/schedules,
      normalizes cross-currency (EUR↔USD), and flags drifts >$2 tolerance.
      With --fix: auto-replaces wrong prices in HTML when displayed < 50% of API
      (clearly undercharging). Writes audit_log entries per site.

  viator-cli health [--compact] [--json]
      Fleet dashboard: active/stale/gone products, drifts, per-site counts,
      availability tracking. Exit 1 if issues found.

  viator-cli sql "SELECT product_code, title FROM products WHERE ..."
      Raw SQL against the local mirror. Read-only enforced.
      For large IN clause workarounds, use scratch tables (_-prefixed):
        viator-cli sql "CREATE TABLE _codes (code TEXT);"
        viator-cli sql "INSERT INTO _codes VALUES ('code1'),('code2'),...;"
        viator-cli sql "SELECT p.* FROM products p JOIN _codes c ON p.product_code=c.code;"
        viator-cli sql "DROP TABLE IF EXISTS _codes;"

  viator-cli doctor
      Self-diagnostic: DB health, API key status, product counts, FTS5 state,
      registry integration. Exit 1 if any issue found.

  viator-cli --profile hanumanhermes bulk-sync [--site onsenexperiences] [--dry-run]
      Bulk-sync all 8 Hanumanhermes sites via freetext search. Reads the
      8-site config (onsenexperiences, citydaytrips, thailandaytours, faithpilgrimage,
      glaciericetours, vinesandplates, frasercoastadventures, reefandrod), paginates
      through freetext results per destination, rate-limited (50ms/page, 2s/destId).
      Per-site reporting with new/updated tracking and site_products mapping.
      Supports --dry-run for estimation and --site for single-site operation.

DESIGN RULES
  - Default output is compact, one-line-per-result, pipe-safe
  - Auto-detects pipe: JSON output when stdout isn't a terminal
  - --compact drops to product_code + title only (70-80% fewer tokens)
  - --json for explicit structured output (full objects/arrays)
  - --csv for spreadsheet consumption (overrides --json)
  - Priority: --compact > --csv > --json > auto-detect > human-readable
  - Typed exit codes: 0=ok, 1=drifts/issues found, 2=usage error, 3=not found,
    4=auth failure, 5=API failure, 7=rate limited
  - --dry-run for safe exploration on all mutating commands
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VERSION = "0.6.0"

# ── Profile Config ─────────────────────────────────────────────────────────
# Each profile defines its DB path, API key source, partner params, and
# preferred API endpoint. Default (no --profile) = Saraswati's fleet.
PROFILES = {
    "hanumanhermes": {
        "db_dir": "profiles/hanumanhermes/data",
        "env_path": "profiles/hanumanhermes/.env",
        "partner_id": "P00299531",
        "mcid": "42383",
        "api_endpoint": "freetext",  # /partner/search/freetext
    },
}

# ── Hanumanhermes 8-Site Fleet ─────────────────────────────────────────────
# Destination-based sites use dest_ids + destination name as search term.
# Text-search sites use descriptive terms (thailandaytours, glaciericetours).
# faithpilgrimage uses day tour filtering via "day tour" search term vs destIds.
HANUMANHERMES_SITES = {
    "onsenexperiences": {
        "domain": "onsenexperiences.com",
        "queries": [
            {"search_term": "Tokyo", "dest_id": "334", "dest_name": "Tokyo"},
            {"search_term": "Kyoto", "dest_id": "332", "dest_name": "Kyoto"},
            {"search_term": "Hakone", "dest_id": "25550", "dest_name": "Hakone"},
            {"search_term": "Beppu", "dest_id": "4659", "dest_name": "Beppu"},
            {"search_term": "Japan", "dest_id": "937", "dest_name": "Japan"},
        ],
        "description": "Japan onsen/hot spring experiences",
    },
    "citydaytrips": {
        "domain": "citydaytrips.com",
        "queries": [
            {"search_term": "Paris", "dest_id": "479", "dest_name": "Paris"},
            {"search_term": "London", "dest_id": "737", "dest_name": "London"},
            {"search_term": "Rome", "dest_id": "511", "dest_name": "Rome"},
            {"search_term": "Lisbon", "dest_id": "538", "dest_name": "Lisbon"},
            {"search_term": "Tokyo", "dest_id": "334", "dest_name": "Tokyo"},
        ],
        "description": "City day trips worldwide",
    },
    "thailandaytours": {
        "domain": "thailandaytours.com",
        "queries": [
            {"search_term": "Bangkok", "dest_id": None, "dest_name": "Bangkok"},
            {"search_term": "Phuket", "dest_id": None, "dest_name": "Phuket"},
            {"search_term": "Chiang Mai", "dest_id": None, "dest_name": "Chiang Mai"},
        ],
        "description": "Thailand tours by day",
    },
    "faithpilgrimage": {
        "domain": "faithpilgrimage.com",
        "queries": [
            {"search_term": "Jerusalem day tour", "dest_id": "921", "dest_name": "Jerusalem"},
            {"search_term": "Varanasi day tour", "dest_id": "929", "dest_name": "Varanasi"},
        ],
        "description": "Faith pilgrimage day tours",
    },
    "glaciericetours": {
        "domain": "glaciericetours.com",
        "queries": [
            {"search_term": "Iceland glacier tour", "dest_id": None, "dest_name": "Iceland"},
            {"search_term": "Alaska glacier tour", "dest_id": None, "dest_name": "Alaska"},
            {"search_term": "ice cave tour", "dest_id": None, "dest_name": "Ice Cave"},
        ],
        "description": "Glacier and ice cave tours",
    },
    "vinesandplates": {
        "domain": "vinesandplates.com",
        "queries": [
            {"search_term": "Adelaide wine tour", "dest_id": "376", "dest_name": "Adelaide"},
            {"search_term": "Perth wine tour", "dest_id": "389", "dest_name": "Perth"},
            {"search_term": "Melbourne wine tour", "dest_id": "384", "dest_name": "Melbourne"},
            {"search_term": "Hunter Valley wine", "dest_id": "357", "dest_name": "Hunter Valley"},
            {"search_term": "Margaret River", "dest_id": "24851", "dest_name": "Margaret River"},
            {"search_term": "Barossa wine tour", "dest_id": "5623", "dest_name": "Barossa"},
        ],
        "description": "Wine and food tours",
    },
    "frasercoastadventures": {
        "domain": "frasercoastadventures.com",
        "queries": [
            {"search_term": "Fraser Island", "dest_id": "366", "dest_name": "Fraser Island"},
            {"search_term": "Hervey Bay", "dest_id": "22028", "dest_name": "Hervey Bay"},
        ],
        "description": "Fraser Coast adventures",
    },
    "reefandrod": {
        "domain": "reefandrod.com",
        "queries": [
            {"search_term": "Cairns reef tour", "dest_id": "754", "dest_name": "Cairns"},
            {"search_term": "Whitsundays", "dest_id": "318", "dest_name": "Whitsundays"},
            {"search_term": "Exmouth", "dest_id": "4494", "dest_name": "Exmouth"},
            {"search_term": "Port Stephens", "dest_id": "759", "dest_name": "Port Stephens"},
        ],
        "description": "Reef and rod fishing/diving tours",
    },
}

# Paths — resolved after profile selection in resolve_profile()
HERMES_DIR = os.path.expanduser("~/.hermes")
DB_PATH = os.path.join(HERMES_DIR, "affiliate-crons", "db", "viator_cli.db")
REGISTRY_PATH = os.path.join(HERMES_DIR, "affiliate-crons", "db", "site_registry.db")
ENV_PATH = os.path.join(HERMES_DIR, ".env")
PARTNER_ID = None   # None = use default Viator URL without ?pid=
MCID = None
API_ENDPOINT = "products_search"  # default: /products/search

# ── Constants ───────────────────────────────────────────────────────────────
TOLERANCE_USD = 2.0                # Price drift tolerance after USD normalization
PRICE_FIX_THRESHOLD = 0.15         # Fix when displayed differs from API by >15% in either direction

# ── Schema ─────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    category TEXT,
    subcategory TEXT,
    destination_id INTEGER,
    destination_name TEXT,
    duration TEXT,
    rating REAL,
    review_count INTEGER,
    last_synced TEXT,
    active INTEGER DEFAULT 1,
    is_available INTEGER DEFAULT 1,
    last_available TEXT,
    last_api_check TEXT,
    from_price REAL,
    product_url TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount REAL NOT NULL,
    usd_amount REAL,
    source TEXT DEFAULT 'api',
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (product_code) REFERENCES products(product_code)
);

CREATE TABLE IF NOT EXISTS site_products (
    site_id TEXT NOT NULL,
    product_code TEXT NOT NULL,
    page_url TEXT,
    html_price REAL,
    html_currency TEXT,
    last_html_check TEXT,
    PRIMARY KEY (site_id, product_code)
);

CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
    title, description, destination_name,
    content=products, content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS products_ai AFTER INSERT ON products BEGIN
    INSERT INTO products_fts(rowid, title, description, destination_name)
    VALUES (new.rowid, new.title, new.description, new.destination_name);
END;

CREATE TRIGGER IF NOT EXISTS products_ad AFTER DELETE ON products BEGIN
    INSERT INTO products_fts(products_fts, rowid, title, description, destination_name)
    VALUES ('delete', old.rowid, old.title, old.description, old.destination_name);
END;

CREATE TRIGGER IF NOT EXISTS products_au AFTER UPDATE ON products BEGIN
    INSERT INTO products_fts(products_fts, rowid, title, description, destination_name)
    VALUES ('delete', old.rowid, old.title, old.description, old.destination_name);
    INSERT INTO products_fts(rowid, title, description, destination_name)
    VALUES (new.rowid, new.title, new.description, new.destination_name);
END;

CREATE TABLE IF NOT EXISTS sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT,
    error_type TEXT NOT NULL,
    error_message TEXT,
    http_status INTEGER,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_prices_code_date ON prices(product_code, recorded_at);
CREATE INDEX IF NOT EXISTS idx_site_products_scan ON site_products(site_id);
"""

EXIT_OK = 0
EXIT_FOUND = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_AUTH = 4
EXIT_API = 5
EXIT_RATE = 7

# ── Pipe detection ──────────────────────────────────────────────────────────
def is_piped():
    return not os.isatty(sys.stdout.fileno())

# ── API helpers ──────────────────────────────────────────────────────────────
def get_viator_key():
    key = os.environ.get("VIATOR_API_KEY", "")
    if key:
        return key
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                if line.startswith("VIATOR_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def resolve_profile(profile_name):
    """Apply profile-specific paths, API key source, and partner config.

    Call at startup before any DB/API operations. Without a profile,
    uses Saraswati's default fleet config (backward compatible).
    """
    global DB_PATH, REGISTRY_PATH, ENV_PATH, PARTNER_ID, MCID, API_ENDPOINT
    if not profile_name:
        return  # defaults already set at module level
    cfg = PROFILES.get(profile_name)
    if not cfg:
        print(f"Unknown profile: {profile_name}", file=sys.stderr)
        print(f"Known: {', '.join(PROFILES)}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    DB_PATH = os.path.join(HERMES_DIR, cfg["db_dir"], "viator_cli.db")
    REGISTRY_PATH = os.path.join(HERMES_DIR, cfg["db_dir"], "site_registry.db")
    ENV_PATH = os.path.join(HERMES_DIR, cfg["env_path"])
    PARTNER_ID = cfg.get("partner_id")
    MCID = cfg.get("mcid")
    API_ENDPOINT = cfg.get("api_endpoint", "products_search")
    # Clear process-level API key so profile's .env takes priority.
    # Prevents --profile hanumanhermes from silently using Saraswati's
    # key when VIATOR_API_KEY is set in the shell environment.
    os.environ.pop("VIATOR_API_KEY", None)
    # Ensure profile data directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def _build_product_url(product_code: str, dest_id=None, title: str = "") -> str:
    """Build Viator product URL with partner params if configured."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80] if title else "tour"
    d_part = f"d{dest_id}-" if dest_id else ""
    url = f"https://www.viator.com/tours/{slug}/{d_part}{product_code}"
    params = []
    if PARTNER_ID:
        params.append(f"pid={PARTNER_ID}")
        params.append(f"mcid={MCID or ''}")
        params.append("medium=link")
    if params:
        url += "?" + "&".join(params)
    return url


# ── Freetext Search (for hanumanhermes and other freetext profiles) ──────
def viator_freetext_search(search_term, dest_id=None, start=1, count=50):
    """Search Viator via /partner/search/freetext endpoint.

    Returns (data, None) or (None, error_tuple).
    Response: data["products"]["results"], data["products"]["totalCount"]
    """
    body = {
        "searchTerm": search_term,
        "searchTypes": [{"searchType": "PRODUCTS", "pagination": {"start": start, "count": count}}],
        "currency": "USD",
    }
    if dest_id:
        body["productFiltering"] = {"destination": str(dest_id)}
    return viator_post("/search/freetext", body)


def sync_product_from_freetext(db, product, site_id=None):
    """Store a product from freetext search results into DB."""
    code = product.get("productCode", "")
    if not code:
        return None
    now = datetime.now(timezone.utc).isoformat()
    title = product.get("title", "Unknown")
    desc = product.get("description", "")
    dests = product.get("destinations", [])
    dest_id = None
    dest_name = ""
    for d in dests:
        if d.get("primary"):
            dest_id = d.get("ref", "")
            break
    if not dest_id and dests:
        dest_id = dests[0].get("ref", "")
    dur = product.get("duration", {}) or {}
    duration_str = ""
    if "fixedDurationInMinutes" in dur:
        duration_str = f"{dur['fixedDurationInMinutes']} min"
    elif "variableDurationFromMinutes" in dur:
        duration_str = f"{dur['variableDurationFromMinutes']}-{dur['variableDurationToMinutes']} min"
    reviews_data = product.get("reviews", {}) or {}
    rating = reviews_data.get("combinedAverageRating")
    review_count = reviews_data.get("totalReviews", 0)
    pricing = product.get("pricing", {}) or {}
    price_summary = pricing.get("summary", {}) or {}
    from_price = price_summary.get("fromPrice")
    price_currency = price_summary.get("currency", "USD")
    db.execute("""
        INSERT OR REPLACE INTO products
        (product_code, title, description, destination_id, destination_name,
         duration, rating, review_count, last_synced, active,
         is_available, last_available, last_api_check)
        VALUES (?,?,?,?,?,?,?,?,?,1,1,?,?)
    """, (code, title, desc, dest_id, dest_name, duration_str,
          rating, review_count, now, now, now))
    if from_price is not None:
        db.execute("""
            INSERT INTO prices (product_code, currency, amount, usd_amount, source, recorded_at)
            VALUES (?,?,?,?,?,?)
        """, (code, price_currency, from_price,
              from_price if price_currency == "USD" else None, "api", now))
    if site_id:
        db.execute("""
            INSERT OR IGNORE INTO site_products (site_id, product_code)
            VALUES (?,?)
        """, (site_id, code))
    return {"code": code, "title": title[:50]}


def get_exchange_rate():
    """Fetch EUR/USD rate. Cached in file for 1 hour. Returns None on failure."""
    cache_path = os.path.join(os.path.dirname(DB_PATH), ".eur_usd_cache")
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < 3600:
            with open(cache_path) as f:
                return float(f.read().strip())
    try:
        url = "https://api.exchangerate-api.com/v4/latest/EUR"
        req = urllib.request.Request(url, headers={"User-Agent": "viator-cli/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        rate = data.get("rates", {}).get("USD")
        if rate:
            with open(cache_path, "w") as f:
                f.write(str(rate))
        return rate
    except Exception:
        return None

def eur_to_usd(eur_amount):
    if eur_amount is None:
        return None
    return round(float(eur_amount) * get_exchange_rate(), 2)

def normalize_to_usd(price, currency, rate):
    """Convert price to USD if needed."""
    if currency == "USD":
        return price
    if currency == "EUR" and rate:
        return round(price * rate, 2)
    return None

def viator_get(path, params=None):
    """Call Viator API. Returns (data, None) or (None, error)."""
    key = get_viator_key()
    if not key:
        return None, ("auth", "VIATOR_API_KEY not set")
    url = f"https://api.viator.com/partner{path}"
    headers = {
        "exp-api-key": key,
        "Accept-Language": "en",
        "Accept": "application/json;version=2.0"
    }
    # Use a 15s timeout (covers connect + read). Bad codes are now purged
    # from the registry, and 400/404 errors skip retries — no more stalls.
    for attempt in range(2):  # 2 attempts max (was 3 — 400/404 don't need retries)
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read()), None
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limit — retry with backoff
                if attempt < 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                return None, ("rate", f"HTTP {e.code}: rate limited after retries")
            if e.code == 400:  # bad request — no retry
                return None, ("api", f"HTTP {e.code}: Bad Request (invalid product code)")
            if e.code in (401, 403):
                return None, ("auth", f"HTTP {e.code}: {e.reason} — check VIATOR_API_KEY")
            if e.code == 404:
                return None, ("api", f"HTTP {e.code}: Not Found")
            # Other server errors (5xx) — retry once
            if attempt < 1:
                time.sleep(2)
                continue
            return None, ("api", f"HTTP {e.code}: {e.reason}")
        except Exception as e:
            if attempt < 1:
                time.sleep(2)
                continue
            return None, ("api", str(e))
    return None, ("api", "Max retries exceeded")


def viator_post(path, body):
    """POST to Viator API with JSON body. Returns (data, None) or (None, error)."""
    key = get_viator_key()
    if not key:
        return None, ("auth", "VIATOR_API_KEY not set")
    url = f"https://api.viator.com/partner{path}"
    headers = {
        "exp-api-key": key,
        "Accept-Language": "en",
        "Accept": "application/json;version=2.0",
        "Content-Type": "application/json",
    }
    data_bytes = json.dumps(body).encode("utf-8")
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=data_bytes, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read()), None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt < 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                return None, ("rate", f"HTTP {e.code}: rate limited after retries")
            if e.code == 400:
                err_body = e.read().decode()[:200]
                return None, ("api", f"HTTP {e.code}: Bad Request — {err_body}")
            if e.code in (401, 403):
                return None, ("auth", f"HTTP {e.code}: {e.reason} — check VIATOR_API_KEY")
            if e.code == 404:
                return None, ("api", f"HTTP {e.code}: Not Found")
            if attempt < 1:
                time.sleep(2)
                continue
            return None, ("api", f"HTTP {e.code}: {e.reason}")
        except Exception as e:
            if attempt < 1:
                time.sleep(2)
                continue
            return None, ("api", str(e))
    return None, ("api", "Max retries exceeded")


def get_destinations():
    """Fetch all destinations from /destinations.
    Returns (lookup_dict, type_dict) — lookup maps destId->destName, type maps destId->type.
    """
    data, err = viator_get("/destinations")
    if err:
        print(f"  [!] Failed to fetch destinations: {err}", file=sys.stderr)
        return {}, {}
    destinations = data.get("destinations", [])
    lookup = {}
    types = {}
    for d in destinations:
        did = str(d.get("destinationId", ""))
        name = d.get("name", "")
        dtype = d.get("type", "")
        if did and name:
            lookup[did] = name
        if did:
            types[did] = dtype
    return lookup, types


def search_products_by_destination(dest_id, start=1, count=50):
    """Search products for a destination with pagination.
    Returns (data, error) — data has keys 'products' and 'totalCount'.
    """
    body = {
        "filtering": {"destination": dest_id},
        "sorting": {"sortOrder": "TOP_SELLER"},
        "pagination": {"start": start, "count": count},
        "currency": "USD",
    }
    return viator_post("/products/search", body)


def viator_get_availability(code):
    """Get price from availability/schedules endpoint (same as old price_check.py)."""
    key = get_viator_key()
    if not key:
        return None, None, "VIATOR_API_KEY not set"
    bare_code = re.sub(r'^d\d+-', '', code)
    url = f"https://api.viator.com/partner/availability/schedules/{bare_code}"
    headers = {
        "Accept": "application/json;version=2.0",
        "Accept-Language": "en-US",
        "exp-api-key": key,
    }
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            summary = data.get("summary", {})
            price = summary.get("fromPrice")
            currency = data.get("currency", "USD")
            return price, currency, None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt < 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                return None, "RATE_LIMITED", f"HTTP 429"
            if e.code == 404:
                return None, "STALE_404", None
            if attempt < 1:
                time.sleep(2)
                continue
            return None, None, f"HTTP {e.code}"
        except Exception as e:
            if attempt < 1:
                time.sleep(2)
                continue
            return None, None, str(e)
    return None, None, "Max retries exceeded"

# ── DB ──────────────────────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("PRAGMA journal_mode=DELETE; PRAGMA foreign_keys=ON;")
    db.executescript(SCHEMA)
    return db

def get_sites(target="all"):
    if not os.path.exists(REGISTRY_PATH):
        return []
    reg = sqlite3.connect(REGISTRY_PATH)
    reg.row_factory = sqlite3.Row
    if target == "all":
        rows = reg.execute(
            "SELECT site_id, local_path, domain FROM sites WHERE status='active'"
        ).fetchall()
    else:
        rows = reg.execute(
            "SELECT site_id, local_path, domain FROM sites WHERE site_id=? AND status='active'",
            (target,)
        ).fetchall()
    reg.close()
    return rows

def update_registry_timestamp(site_id, column):
    """Update last_product_sync_at or last_price_check_at in registry.
    
    Auto-migrates missing columns (no such column → ALTER TABLE ADD COLUMN).
    """
    if not os.path.exists(REGISTRY_PATH):
        return
    reg = sqlite3.connect(REGISTRY_PATH)
    try:
        reg.execute(
            f"UPDATE sites SET {column}=CURRENT_TIMESTAMP WHERE site_id=?",
            (site_id,)
        )
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "no such column" in msg:
            col = msg.split(":")[-1].strip().strip("'\"")
            reg.execute(f"ALTER TABLE sites ADD COLUMN {col} TEXT")
            reg.execute(
                f"UPDATE sites SET {column}=CURRENT_TIMESTAMP WHERE site_id=?",
                (site_id,)
            )
        else:
            raise
    reg.commit()
    reg.close()

def write_audit_log(site_id, check_type, details, issues_found=0, status="ok"):
    """Write audit entry to site_registry.db."""
    if not os.path.exists(REGISTRY_PATH):
        return
    reg = sqlite3.connect(REGISTRY_PATH)
    reg.execute(
        "INSERT INTO audit_log (site_id, check_type, status, issues_found, details) VALUES (?,?,?,?,?)",
        (site_id, check_type, status, issues_found, details)
    )
    reg.commit()
    reg.close()

# ── HTML Price Extraction (from price_check.py) ─────────────────────────────

def resolve_html_path(local_path, page_url):
    """Resolve the HTML file path from site local_path + page_url.

    Handles cleanUrls (.html extension) and directory-page edge cases.
    """
    rel = page_url.lstrip("/")
    if not rel:
        return None

    base = os.path.join(local_path, rel)

    # Direct file hit
    if os.path.isfile(base):
        return base

    # With .html extension
    base_html = base + ".html"
    if os.path.isfile(base_html):
        return base_html

    # If it's a directory, try index.html inside it
    if os.path.isdir(base):
        index = os.path.join(base, "index.html")
        if os.path.isfile(index):
            return index

    # Try /index.html path
    if rel.endswith("/"):
        index2 = os.path.join(local_path, rel.rstrip("/"), "index.html")
        if os.path.isfile(index2):
            return index2

    return None


def find_price_near_product_code(html, base_code, return_match=False):
    """Find the displayed price for a Viator product code in the HTML.

    Strategy: find ALL <a> tags whose href contains the product code,
    but only consider anchors with class='cta-button' or 'cta-btn'.
    Body-text cross-references to other products inside adjacent cards
    are skipped to prevent cascading price contamination.

    If return_match=True, returns (price, currency, match_start, match_end)
    so the caller can replace the price text. Otherwise returns (price, currency) or (None, None).
    """
    if base_code not in html:
        return (None, None, None, None) if return_match else (None, None)

    # Find all anchor tags containing this product code in href
    anchor_pattern = re.compile(
        r'<a[^>]*?href="[^"]*?' + re.escape(base_code) + r'[^"]*"[^>]*>.*?</a>',
        re.IGNORECASE
    )

    cta_prices = []      # Non-return_match: (card_start, price, currency)
    cta_matches = []     # return_match: (price, currency, match_start, match_end)

    for anchor_match in anchor_pattern.finditer(html):
        anchor_html = anchor_match.group(0)
        anchor_start = anchor_match.start()
        is_cta = 'cta-button' in anchor_html or 'cta-btn' in anchor_html

        if not is_cta:
            continue  # Skip cross-reference anchors — only CTA buttons count

        # Find the containing card div: go backwards from anchor
        card_starts = [m.start() for m in re.finditer(
            r'<div class="(?:comp-card|product-card|tour-review-card)(?: |")', html[:anchor_start])]
        if not card_starts:
            continue
        card_start = card_starts[-1]

        # Verify anchor is inside this card div
        level = 1
        card_closed_at = None
        tag_end = html.index('>', card_start) + 1
        for m in re.finditer(r'</?div[\s>]', html[tag_end:]):
            if m.group(0).startswith('</'):
                level -= 1
            else:
                level += 1
            if level == 0:
                card_closed_at = tag_end + m.end()
                break
        if card_closed_at is not None and anchor_start > card_closed_at:
            continue

        # Find the price within this card
        card_html = html[card_start:anchor_start]
        price_match = None
        for cls in ('comp-card-price', 'product-card-price', 'price', 'tour-price'):
            price_regex = (r'<[^>]*class="[^"]*' + cls + r'[^"]*"[^>]*>'
                           r'(?:From\s+|from\s+)?([€$])(\d+(?:\.\d{2})?)[^<]*<')
            price_match = re.search(price_regex, card_html)
            if price_match:
                break
        if price_match:
            sym, amt = price_match.group(1), price_match.group(2)
            curr = "USD" if sym == "$" else "EUR"
            if return_match:
                match_start = card_start + price_match.start()
                match_end = card_start + price_match.end()
                cta_matches.append((float(amt), curr, match_start, match_end))
            else:
                cta_prices.append((card_start, float(amt), curr))

    if cta_matches:
        return cta_matches[0]
    if cta_prices:
        return cta_prices[0][1], cta_prices[0][2]

    # No CTA anchor for this product code → cross-references only.
    # Don't return prices from other products' cards.
    return (None, None, None, None) if return_match else (None, None)


def get_displayed_price(html_path, base_code, return_match=False):
    """Extract the displayed price from HTML near a product card."""
    if not html_path:
        return (None, None) if not return_match else (None, None, None, None)
    path = Path(html_path)
    if not path.exists():
        return (None, None) if not return_match else (None, None, None, None)
    html = path.read_text(errors="ignore")
    return find_price_near_product_code(html, base_code, return_match=return_match)


def has_product_card_price(html_path, base_code):
    """Check if a product has a displayed price in a product card."""
    if not html_path:
        return False
    path = Path(html_path)
    if not path.exists():
        return False
    html = path.read_text(errors="ignore")
    price, _ = find_price_near_product_code(html, base_code)
    return price is not None


def fix_price_in_html(html_path, match_start, match_end, new_price, currency_symbol):
    """Replace a displayed price in HTML with the API price.

    Preserves the 'From ' prefix and currency symbol. Only replaces the amount.
    Returns True if the file was modified.
    """
    path = Path(html_path)
    html = path.read_text(errors="ignore")
    matched = html[match_start:match_end]

    price_re = re.compile(r'((?:From\s+|from\s+)?)([€$])(\d+(?:\.\d{2})?)')
    m = price_re.search(matched)
    if not m:
        snippet = matched[:80].replace('\n', ' ')
        print(f"  ⚠ FIX SKIPPED: price regex no match in fragment: {snippet}...", file=sys.stderr)
        return False

    prefix = m.group(1)
    symbol = m.group(2)
    new_amount = f"{new_price:.2f}"
    replacement = f"{prefix}{symbol}{new_amount}"

    if replacement == m.group(0):
        print(f"  ⚠ FIX SKIPPED: price already correct ({replacement})", file=sys.stderr)
        return False

    # Adjust positions: matched text may include HTML tag before the price
    actual_start = match_start + m.start()
    actual_end = match_start + m.end()
    new_html = html[:actual_start] + replacement + html[actual_end:]
    path.write_text(new_html)

    # Verify the write actually took effect
    verify_html = path.read_text(errors="ignore")
    if replacement not in verify_html[actual_start:actual_start + len(replacement) + 20]:
        print(f"  ⚠ FIX VERIFY FAILED: {replacement} not found after write in {html_path}", file=sys.stderr)
        return False

    return True


# ── Output helpers ──────────────────────────────────────────────────────────
def output_result(rows, args, cols_fn=None):
    """Unified output with correct priority: explicit flags > auto-detection."""
    dict_rows = [dict(r) for r in rows]

    if args.compact:
        for r in dict_rows:
            code = r.get("product_code", "")
            title = r.get("title", "")
            print(f"{code} {title[:80]}" if title else str(code))
        return
    if args.csv:
        import csv
        w = csv.writer(sys.stdout, quoting=csv.QUOTE_MINIMAL)
        if dict_rows:
            w.writerow(dict_rows[0].keys())
            for r in dict_rows:
                w.writerow(r.values())
        return
    if args.json or is_piped():
        print(json.dumps(dict_rows, default=str))
        return

    # Default human-readable
    for r in dict_rows:
        code = r.get("product_code", "")
        title = r.get("title", "")
        dest = r.get("destination_name", "")
        rating = r.get("rating")
        ratestr = f" {rating}★" if rating else ""
        print(f"{code:14s} {title[:55]:55s} {dest[:20]:20s}{ratestr}")


# ── Sync ────────────────────────────────────────────────────────────────────
def fetch_product_codes(site_id):
    """Get all product codes for a site from the registry."""
    if not os.path.exists(REGISTRY_PATH):
        return []
    reg = sqlite3.connect(REGISTRY_PATH)
    rows = reg.execute(
        "SELECT DISTINCT product_code FROM site_products WHERE site_id=?",
        (site_id,)
    ).fetchall()
    reg.close()
    return [r[0] for r in rows]


def discover_product_codes(site_path):
    """Extract all unique Viator product codes from HTML files in a site directory."""
    codes = set()
    VALID_CODE = re.compile(r'^\d+P\d+$')
    for root, dirs, files in os.walk(site_path):
        SKIP = {'.git', 'node_modules', 'css', 'images', 'backup'}
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('.') and not d.startswith('backup_nav_')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            try:
                with open(os.path.join(root, fn), 'r') as f:
                    html = f.read()
            except Exception:
                continue
            # Extract from /dDESTID-CODE and /tours/CODE patterns
            for m in re.finditer(r'/d\d+-?(\w+)(?:[?/]|$)', html):
                code = m.group(1)
                if code.lower() != 'ttd' and VALID_CODE.match(code):
                    codes.add(code)
            for m in re.finditer(r'/tours/([A-Za-z0-9]+)(?:[?/]|$)', html):
                code = m.group(1)
                if code.lower() != 'ttd' and VALID_CODE.match(code):
                    codes.add(code)
    return codes


def sync_product(db, code, exchange_rate):
    """Fetch one product from Viator and store in DB."""
    now = datetime.now(timezone.utc).isoformat()

    # Product info
    data, err = viator_get(f"/products/{code}")
    if err:
        err_type, err_msg = err
        db.execute(
            "INSERT INTO sync_errors (product_code, error_type, error_message, recorded_at) VALUES (?,?,?,?)",
            (code, err_type, str(err_msg)[:500], now))
        # BUGFIX: mark as dead when product endpoint returns 404
        if err_type == "api" and "404" in str(err_msg):
            db.execute("UPDATE products SET active=0, is_available=0 WHERE product_code=?", (code,))
            return {"status": "gone", "code": code}
        return {"status": "error", "code": code, "error": err}

    title = data.get("title", "Unknown")
    desc = data.get("description", "")
    # Extract destination from destinations array (v2 API shape)
    dests = data.get("destinations", [])
    dest_id = None
    if dests:
        dest_id = dests[0].get("ref") if isinstance(dests[0], dict) else None
    dest_name = ""  # Not returned by /products/{code} — will be backfilled
    # Duration: v2 API returns an object like {"fixedDurationInMinutes":150}
    duration = data.get("duration", "")
    if isinstance(duration, dict):
        if "fixedDurationInMinutes" in duration:
            duration = f"{duration['fixedDurationInMinutes']} min"
        elif "variableDurationFromMinutes" in duration:
            duration = f"{duration['variableDurationFromMinutes']}-{duration['variableDurationToMinutes']} min"
        else:
            duration = ""
    # Viator API v2 nests rating inside reviews object
    reviews_data = data.get("reviews", {}) or {}
    rating = reviews_data.get("combinedAverageRating")
    total_reviews = reviews_data.get("totalReviews", 0)
    # v2 API doesn't return category/subcategory at top level
    cat = ""
    subcat = ""

    # productUrl — canonical Viator URL (v2 API shape)
    product_url = data.get("productUrl", "")

    now = datetime.now(timezone.utc).isoformat()

    # Use UPSERT (ON CONFLICT) to preserve existing from_price
    # when availability hasn't been called yet.
    db.execute("""
        INSERT INTO products
        (product_code, title, description, category, subcategory, destination_id,
         destination_name, duration, rating, review_count, last_synced, active,
         is_available, last_available, last_api_check, product_url)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,1,1,?,?,?)
        ON CONFLICT(product_code) DO UPDATE SET
            title=excluded.title,
            description=excluded.description,
            category=excluded.category,
            subcategory=excluded.subcategory,
            destination_id=excluded.destination_id,
            duration=excluded.duration,
            rating=excluded.rating,
            review_count=excluded.review_count,
            last_synced=excluded.last_synced,
            active=excluded.active,
            is_available=excluded.is_available,
            last_available=excluded.last_available,
            last_api_check=excluded.last_api_check,
            product_url=excluded.product_url
    """, (code, title, desc, cat, subcat, dest_id, dest_name, duration,
          rating, total_reviews, now, now, now, product_url))

    # Price from availability endpoint
    api_price, api_currency, api_err = viator_get_availability(code)
    if api_price is not None:
        usd = normalize_to_usd(api_price, api_currency, get_exchange_rate())
        if usd is None:
            # Currency not supported — store price but don't normalize
            db.execute("""
                INSERT INTO prices (product_code, currency, amount, usd_amount, source, recorded_at)
                VALUES (?,?,?,?,?,?)
            """, (code, api_currency, api_price, None, "api", now))
            return {"status": "ok", "code": code, "title": title[:50]}
        db.execute("""
            INSERT INTO prices (product_code, currency, amount, usd_amount, source, recorded_at)
            VALUES (?,?,?,?,?,?)
        """, (code, api_currency, api_price, usd, "api", now))
        # Populate from_price on the product row itself (for cards/audit display)
        db.execute("UPDATE products SET from_price=? WHERE product_code=?", (usd, code))
        return {"status": "ok", "code": code, "title": title[:50]}
    elif api_currency == "STALE_404":
        db.execute("UPDATE products SET active=0, is_available=0 WHERE product_code=?", (code,))
        return {"status": "gone", "code": code}
    elif api_currency == "RATE_LIMITED":
        return {"status": "rate_limited", "code": code}
    else:
        db.execute(
            "INSERT INTO sync_errors (product_code, error_type, error_message, recorded_at) VALUES (?,?,?,?)",
            (code, "availability", str(api_err)[:500], now))
        return {"status": "error", "code": code, "error": api_err}


def cmd_errors(args):
    """Show recent sync errors."""
    db = get_db()
    sql = "SELECT id, product_code, error_type, error_message, recorded_at FROM sync_errors"
    params = []
    if args.since:
        sql += " WHERE recorded_at >= ?"
        params.append(args.since)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)

    rows = db.execute(sql, params).fetchall()
    if not rows:
        print("No sync errors recorded.")
        return

    print(f"{'ID':<6} {'Code':<12} {'Type':<14} {'Message':<60} {'Recorded'}")
    print("-" * 110)
    for r in rows:
        msg = (r[3] or "")[:57]
        print(f"{r[0]:<6} {r[1] or '':<12} {r[2]:<14} {msg:<60} {r[4]}")
    print(f"\n{len(rows)} error(s) shown.")

def cmd_sync(args):
    """Sync product catalog from Viator API."""
    db = get_db()
    exchange_rate = get_exchange_rate()

    if args.discover:
        # Discover codes from HTML files across all configured site directories
        all_codes = set()
        for s in get_sites("all"):
            site_path = os.path.expanduser(s["local_path"])
            if os.path.isdir(site_path):
                discovered = discover_product_codes(site_path)
                all_codes.update(discovered)
        codes = list(all_codes)
        print(f"Discovered {len(codes)} unique product codes from fleet HTML files", file=sys.stderr)
    else:
        sites = get_sites(args.site)
        if not sites:
            print(f"No active sites found for target '{args.site}'", file=sys.stderr)
            sys.exit(EXIT_NOT_FOUND)

        all_codes = set()
        for s in sites:
            codes_from_reg = fetch_product_codes(s["site_id"])
            all_codes.update(codes_from_reg)
        codes = list(all_codes)

    total = len(codes)
    if total == 0:
        print("No products to sync", file=sys.stderr)
        sys.exit(EXIT_OK)

    if args.dry_run:
        print(f"[DRY-RUN] Would sync {total} product(s)", file=sys.stderr)
        for code in sorted(codes):
            print(f"  {code}")
        sys.exit(EXIT_OK)

    print(f"Syncing {total} product(s)...", file=sys.stderr)

    ok = errors = gone = rate_limited = 0
    for i, code in enumerate(sorted(codes)):
        result = sync_product(db, code, exchange_rate)
        if result["status"] == "ok":
            ok += 1
        elif result["status"] == "gone":
            gone += 1
        elif result["status"] == "rate_limited":
            rate_limited += 1
            errors += 1
        else:
            errors += 1

        if args.verbose and (i + 1) % 25 == 0:
            print(f"  {i+1}/{total}", file=sys.stderr)
        if rate_limited > 5:
            print(f"  [RATE-LIMITED] Too many requests; pausing 30s...", file=sys.stderr)
            time.sleep(30)
            rate_limited = 0
        time.sleep(0.05)

    # Update registry timestamps (skip when discovering — no site context)
    if not args.discover:
        for s in sites:
            update_registry_timestamp(s["site_id"], "last_product_sync_at")

    print(f"synced={ok} gone={gone} errors={errors} total={total}")
    db.commit()
    # Force checkpoint so writes are visible to other connections
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        print(f"  [checkpoint error: {e}]", file=sys.stderr)
    print(f"  [DB closed: {DB_PATH}]", file=sys.stderr)
    db.close()
    sys.exit(EXIT_FOUND if (gone > 0 or errors > 0) else EXIT_OK)


# ── Catalog Sync (full enumeration via /products/search) ─────────────────
def _store_product_from_search(db, product, dest_lookup):
    """Store a product returned by /products/search into the DB."""
    code = product.get("productCode", "")
    if not code:
        return None
    title = product.get("title", "Unknown")
    desc = product.get("description", "")
    # Extract destination from the destinations array
    dests = product.get("destinations", [])
    dest_id = None
    dest_name = None
    for d in dests:
        if d.get("primary"):
            dest_id = d.get("ref", "")
            dest_name = dest_lookup.get(dest_id, "")
            break
    if not dest_id and dests:
        dest_id = dests[0].get("ref", "")
        dest_name = dest_lookup.get(dest_id, "")
    # Duration string from duration object
    dur = product.get("duration", {}) or {}
    duration_str = ""
    if "fixedDurationInMinutes" in dur:
        duration_str = f"{dur['fixedDurationInMinutes']} min"
    elif "variableDurationFromMinutes" in dur and "variableDurationToMinutes" in dur:
        duration_str = f"{dur['variableDurationFromMinutes']}-{dur['variableDurationToMinutes']} min"
    # Reviews
    reviews_data = product.get("reviews", {}) or {}
    rating = reviews_data.get("combinedAverageRating")
    review_count = reviews_data.get("totalReviews", 0)
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT OR REPLACE INTO products
        (product_code, title, description, destination_id, destination_name,
         duration, rating, review_count, last_synced, active,
         is_available, last_available, last_api_check)
        VALUES (?,?,?,?,?,?,?,?,?,1,1,?,?)
    """, (code, title, desc, dest_id, dest_name, duration_str,
          rating, review_count, now, now, now))
    return {"code": code, "title": title[:50], "dest_name": dest_name}


def cmd_sync_catalog(args):
    """Full catalog sync: enumerate all products via /products/search per destination."""
    dry_run = getattr(args, "dry_run", False)
    max_count = getattr(args, "count", 0)  # 0 = no limit

    # 1. Build destination lookup
    if not dry_run:
        print("Fetching destination catalog...", file=sys.stderr)
    dest_lookup, dest_types = get_destinations()
    if not dest_lookup:
        print("  [!] Empty destination catalog — cannot sync", file=sys.stderr)
        sys.exit(EXIT_API)
    print(f"  Got {len(dest_lookup)} destinations", file=sys.stderr)

    # 2. Decide which destinations to scan
    #    Use COUNTRY and REGION level destinations for broad coverage,
    #    plus any destination IDs already referenced in the DB.
    db = get_db() if not dry_run else None
    known_dest_ids = set()
    if db:
        rows = db.execute("SELECT DISTINCT destination_id FROM products WHERE destination_id IS NOT NULL").fetchall()
        known_dest_ids = {str(r["destination_id"]) for r in rows}

    # Choose all COUNTRY, REGION, plus any known dest IDs
    scan_ids = set()
    for did, dtype in dest_types.items():
        if dtype in ("COUNTRY", "REGION", "MEGAREGION"):
            scan_ids.add(did)
    scan_ids.update(known_dest_ids)

    # Sort by destination name for deterministic ordering
    sorted_dests = sorted(scan_ids, key=lambda x: dest_lookup.get(x, x))
    print(f"  Will scan {len(sorted_dests)} destinations (all countries/regions + known)", file=sys.stderr)
    if args.verbose:
        for did in sorted_dests:
            print(f"    {did:>8s}  {dest_lookup.get(did, '?')}", file=sys.stderr)

    if dry_run:
        total_est = 0
        for did in sorted_dests:
            res, err = search_products_by_destination(did, start=1, count=1)
            if err:
                if args.verbose:
                    print(f"  [!] {did} {dest_lookup.get(did,'')}: {err}", file=sys.stderr)
                continue
            tc = res.get("totalCount", 0)
            total_est += tc
            print(f"  {did:>8s}  {dest_lookup.get(did,''):35s}  ~{tc} products")
        print(f"\n[DRY-RUN] Estimated {total_est} products across {len(sorted_dests)} destinations", file=sys.stderr)
        print(f"[DRY-RUN] Would store: product_code, title, description, destination_id, destination_name, duration, rating", file=sys.stderr)
        sys.exit(EXIT_OK)

    # 3. Iterate destinations, paginate through products
    total_products = 0
    total_inserted = 0
    total_errors = 0
    batch_count = 0

    for dest_idx, did in enumerate(sorted_dests):
        dest_name = dest_lookup.get(did, "?")
        page = 1
        per_page = 50
        dest_products = 0

        while True:
            res, err = search_products_by_destination(did, start=page, count=per_page)
            if err:
                print(f"  [!] {did} {dest_name} page {page}: {err}", file=sys.stderr)
                total_errors += 1
                break

            products = res.get("products", [])
            tc = res.get("totalCount", 0)
            if not products:
                break

            for prod in products:
                result = _store_product_from_search(db, prod, dest_lookup)
                if result:
                    total_products += 1
                    dest_products += 1

            # Commit every 100 products across all destinations
            batch_count += len(products)
            if batch_count >= 100:
                db.commit()
                batch_count = 0
                if args.verbose:
                    print(f"  ... {total_products} products so far", file=sys.stderr)

            # Next page
            page_start = (page - 1) * per_page + 1
            if page_start + len(products) > tc:
                break
            page += 1
            time.sleep(0.1)  # Brief pause between pages

        if args.verbose:
            print(f"  [{dest_idx+1}/{len(sorted_dests)}] {did:>8s} {dest_name:35s}  {dest_products} products", file=sys.stderr)

    # Final commit
    if batch_count > 0:
        db.commit()
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

    print(f"catalog: {total_products} products synced from {len(sorted_dests)} destinations, {total_errors} errors", file=sys.stderr)
    db.close()
    sys.exit(EXIT_FOUND if total_errors > 0 else EXIT_OK)


# ── Query ───────────────────────────────────────────────────────────────────
def cmd_products(args):
    """Query products from local mirror."""
    db = get_db()
    where = ["p.active = 1"]
    params = []

    # Track whether we're doing an FTS5 search (changes query structure + ordering)
    use_fts_join = bool(args.search)

    if args.site:
        where.append("p.product_code IN (SELECT product_code FROM site_products WHERE site_id = ?)")
        params.append(args.site)

    if use_fts_join:
        # FTS5 JOIN approach — enables rank-based ordering (relevance)
        safe = re.sub(r'[^a-zA-Z0-9\s]', '', args.search)
        fts_params = [safe if safe else args.search]
        # Build the FROM/JOIN clause
        from_clause = "products p JOIN products_fts fts ON p.rowid = fts.rowid"
        where.append("fts.products_fts MATCH ?")
        params = fts_params + params  # FTS5 match param first
        order_clause = "ORDER BY fts.rank"
    else:
        from_clause = "products p"
        order_clause = "ORDER BY p.destination_name, p.title"

    if args.destination:
        where.append("p.destination_name LIKE ?")
        params.append(f"%{args.destination}%")
    if args.price_dropped is not None:
        where.append("""
            p.product_code IN (
                SELECT a.product_code FROM prices a
                JOIN prices b ON a.product_code = b.product_code
                    AND b.id = (SELECT MAX(id) FROM prices WHERE product_code = a.product_code)
                    AND a.id = (SELECT MAX(id) FROM prices WHERE product_code = a.product_code AND id < b.id)
                WHERE a.usd_amount > 0 AND b.usd_amount > 0
                    AND ((a.usd_amount - b.usd_amount) / a.usd_amount * 100) >= ?
            )
        """)
        params.append(float(args.price_dropped))
    if args.stale:
        where.append("p.last_synced < datetime('now', '-7 days')")

    query = f"SELECT p.* FROM {from_clause} WHERE {' AND '.join(where)} {order_clause}"
    if args.limit:
        query += f" LIMIT {int(args.limit)}"

    rows = db.execute(query, params).fetchall()
    output_result(rows, args)
    if rows:
        n = len(rows)
        if not args.json and not args.csv and not args.compact:
            print(f"\n{n} result(s). Narrow with --search, --site, --price-dropped, --limit.", file=sys.stderr)
    sys.exit(EXIT_OK if rows else EXIT_NOT_FOUND)

def cmd_resolve(args):
    """Resolve a search query to a specific product code via FTS5.

    Two-tier FTS5 search: full query first, then activity-only fallback.
    Never returns ttd/g5335/fallback category codes. Replaces old ttd
    reseolve_product_code logic from page_generator.py.
    """
    db = get_db()
    query = args.query.strip()
    if not query:
        print("resolve: query is required", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    # Sanitize for FTS5 (remove special chars that confuse MATCH)
    safe = re.sub(r'[^a-zA-Z0-9\s]', '', query).strip()
    if not safe:
        safe = query  # fall through and let FTS5 handle it

    # Two-tier: full query first, then activity-only fallback
    words = safe.split()
    activity_only = ' '.join(words[1:]) if len(words) > 1 else safe

    BLOCKED = ('ttd', 'ttd-', 'g5335', 'all')
    dest_id = getattr(args, 'dest_id', None)

    def _find_match(search_term, dest_id_filter=None):
        sql = """SELECT p.product_code, p.title, p.destination_id, p.destination_name
                 FROM products_fts
                 JOIN products AS p ON p.rowid = products_fts.rowid
                 WHERE products_fts MATCH ?"""
        params = [search_term]
        if dest_id_filter:
            sql += " AND p.destination_id = ?"
            params.append(dest_id_filter)
        sql += " ORDER BY products_fts.rank LIMIT 1"
        row = db.execute(sql, params).fetchone()
        if row and row['product_code'].lower() not in BLOCKED:
            return row
        return None

    if dest_id:
        # Try with dest-id filter first
        row = _find_match(safe, dest_id_filter=dest_id)
        if not row and activity_only != safe:
            row = _find_match(activity_only, dest_id_filter=dest_id)
        if not row:
            print(f"resolve: no match in region (destId={dest_id}) for '{query}'", file=sys.stderr)
            sys.exit(EXIT_NOT_FOUND)
    else:
        row = _find_match(safe)
        if not row and activity_only != safe:
            row = _find_match(activity_only)

        if not row:
            print(f"resolve: no match found for '{query}'", file=sys.stderr)
            sys.exit(EXIT_NOT_FOUND)

    url = _build_product_url(row['product_code'], row['destination_id'], row['title'])

    if args.json:
        print(json.dumps({
            "code": row['product_code'],
            "title": row['title'],
            "url": url,
            "destId": row['destination_id'],
            "destination_name": row['destination_name'],
        }, default=str))
    elif args.compact:
        print(f"{row['product_code']} | {row['title'][:60]} | {url}")
    else:
        print(f"{row['product_code']} | {row['title']} | {url}")

    db.close()
    sys.exit(EXIT_OK)


def cmd_product(args):
    """Show one product's details with canonical URL."""
    db = get_db()
    row = db.execute("SELECT * FROM products WHERE product_code=?", (args.code,)).fetchone()
    if not row:
        print(f"Product {args.code} not found in local mirror", file=sys.stderr)
        sys.exit(EXIT_NOT_FOUND)

    # Build canonical URL with partner params
    url = _build_product_url(row['product_code'], row['destination_id'], row['title'])

    # Get latest price from prices table
    latest_price = db.execute(
        "SELECT amount, currency, usd_amount FROM prices WHERE product_code=? ORDER BY recorded_at DESC LIMIT 1",
        (args.code,)
    ).fetchone()

    if args.json or is_piped():
        data = {
            "code": row['product_code'],
            "title": row['title'],
            "url": url,
            "destId": row['destination_id'],
            "rating": row['rating'],
        }
        if latest_price:
            data["price"] = {
                "amount": latest_price["amount"],
                "currency": latest_price["currency"],
                "usd_amount": latest_price["usd_amount"],
            }
        if args.prices:
            prices = db.execute(
                "SELECT * FROM prices WHERE product_code=? ORDER BY recorded_at DESC LIMIT 10",
                (args.code,)).fetchall()
            data["price_history"] = [dict(p) for p in prices]
        print(json.dumps(data, default=str))
    elif args.compact:
        price_str = f" ${latest_price['usd_amount']:.2f}" if latest_price and latest_price.get('usd_amount') else ""
        print(f"{row['product_code']} {row['title'][:60]}{price_str}  {url}")
    else:
        print(f"Code:     {row['product_code']}")
        print(f"Title:    {row['title']}")
        print(f"URL:      {url}")
        print(f"Dest:     {row['destination_name']} (destId: {row['destination_id']})")
        if row['rating']:
            print(f"Rating:   {row['rating']}★ ({row['review_count']} reviews)")
        if latest_price:
            usd = f" (${latest_price['usd_amount']:.2f})" if latest_price.get('usd_amount') else ""
            print(f"Price:    {latest_price['currency']} {latest_price['amount']:.2f}{usd}")
        print(f"Available: {'yes' if row['is_available'] else 'GONE' if row['active'] == 0 else 'unknown'}")
        print(f"Synced:   {row['last_synced']}")
        if args.prices:
            prices = db.execute(
                "SELECT * FROM prices WHERE product_code=? ORDER BY recorded_at DESC LIMIT 5",
                (args.code,)).fetchall()
            if prices:
                print(f"\nPrice history (last {len(prices)}):")
                for p in prices:
                    tag = "←html" if p['source'] == 'html' else ""
                    usd = f" (${p['usd_amount']:.2f})" if p.get('usd_amount') and p.get('currency') == 'EUR' else ""
                    print(f"  {p['recorded_at'][:19]}  {p['currency']:>3s} {p['amount']:>8.2f}{usd}  {tag}")
    sys.exit(EXIT_OK)

# ── Price drift ─────────────────────────────────────────────────────────────
def cmd_prices(args):
    """Detect price drift across fleet."""
    db = get_db()
    query = """
        SELECT a.product_code, p.title, p.destination_name,
               a.currency as old_currency, a.amount as old_amount, a.usd_amount as old_usd,
               b.currency as new_currency, b.amount as new_amount, b.usd_amount as new_usd,
               CASE WHEN a.usd_amount > 0 AND b.usd_amount > 0
                    THEN ROUND(((a.usd_amount - b.usd_amount) / a.usd_amount * 100), 1)
                    ELSE NULL END as drift_pct,
               b.recorded_at
        FROM prices a
        JOIN prices b ON a.product_code = b.product_code
            AND b.id = (SELECT MAX(id) FROM prices WHERE product_code = a.product_code)
            AND a.id = (SELECT MAX(id) FROM prices WHERE product_code = a.product_code AND id < b.id)
        JOIN products p ON p.product_code = a.product_code
        WHERE a.usd_amount IS NOT NULL AND b.usd_amount IS NOT NULL
          AND a.usd_amount > 0 AND b.usd_amount > 0
    """
    params = []
    if args.min_pct:
        query += " AND ABS((a.usd_amount - b.usd_amount) / a.usd_amount * 100) >= ?"
        params.append(float(args.min_pct))
    if args.site:
        query += " AND a.product_code IN (SELECT product_code FROM site_products WHERE site_id=?)"
        params.append(args.site)
    query += " ORDER BY ABS((a.usd_amount - b.usd_amount) / a.usd_amount * 100) DESC"
    if args.limit:
        query += f" LIMIT {int(args.limit)}"

    rows = db.execute(query, params).fetchall()

    if not rows:
        if args.json or is_piped():
            print(json.dumps({"drifts": 0}))
        else:
            print("0 drifts")
        sys.exit(EXIT_OK)

    if args.json or is_piped():
        print(json.dumps({"drifts": len(rows), "products": [dict(r) for r in rows]}, default=str))
    elif args.compact:
        for r in rows:
            pct = abs(r['drift_pct'])
            direction = "↑" if r['drift_pct'] < 0 else "↓"
            print(f"{r['product_code']} {direction}{pct:.0f}% ${r['old_usd']:.0f}→${r['new_usd']:.0f}")
    else:
        for r in rows:
            pct = abs(r['drift_pct'])
            direction = "↑" if r['drift_pct'] < 0 else "↓"
            print(f"{r['product_code']:12s} {pct:>+5.1f}% {direction} "
                  f"${r['old_usd']:.0f}→${r['new_usd']:.0f}  {r['title'][:50]}")
        print(f"\n{len(rows)} drifts")

    sys.exit(EXIT_FOUND)

# ── HTML Price Comparison (from price_check.py) ─────────────────────────────

def cmd_compare(args):
    """HTML-vs-API price comparison across fleet sites.

    Extracts displayed prices from HTML product cards, fetches live API prices
    from /availability/schedules, normalizes cross-currency, and flags drifts.
    With --fix: auto-replaces wrong prices in HTML when confidence is high.
    """
    api_key = get_viator_key()
    if not api_key:
        print("ERROR: VIATOR_API_KEY not found", file=sys.stderr)
        sys.exit(EXIT_AUTH)

    eur_rate = get_exchange_rate()
    do_fix = getattr(args, 'fix', False)

    if not args.quiet:
        if eur_rate:
            print(f"Exchange rate: 1 EUR = {eur_rate:.4f} USD", file=sys.stderr)
        else:
            print("[WARN] Could not fetch exchange rate — will only compare same-currency prices", file=sys.stderr)
        print(f"Price Compare — {args.site if args.site else 'all'} — {datetime.now().isoformat()[:19]}\n", file=sys.stderr)

    sites = get_sites(args.site if args.site else "all")
    if not sites:
        print(f"No active sites found", file=sys.stderr)
        sys.exit(EXIT_NOT_FOUND)

    total = {"checked": 0, "mismatches": 0, "api_failures": 0,
             "stale_products": 0, "no_price_found": 0, "fixed": 0,
             "fix_failures": 0, "currency_mismatch": 0, "issues": []}

    for s in sites:
        site_id = s["site_id"]
        local_path = s["local_path"]
        domain = s["domain"]

        # Get product pages from registry
        reg = sqlite3.connect(REGISTRY_PATH)
        pages = reg.execute(
            "SELECT product_code, page_url FROM site_products WHERE site_id=?",
            (site_id,)
        ).fetchall()
        reg.close()

        site_checked = 0
        site_mismatches = 0
        site_stale = 0
        site_no_price = 0
        site_skipped_inline = 0
        site_fixed_local = 0

        for product_code, page_url in pages:
            html_path = resolve_html_path(local_path, page_url)

            # Skip products without displayed card prices
            if not has_product_card_price(html_path, product_code):
                site_skipped_inline += 1
                continue

            # Get API price
            api_price, api_currency, api_err = viator_get_availability(product_code)

            if api_currency == "STALE_404":
                total["stale_products"] += 1
                site_stale += 1
                total["issues"].append(("stale", f"{site_id}: {product_code} — 404 on {page_url}"))
                continue

            if api_price is None:
                total["api_failures"] += 1
                total["issues"].append(("api_error", f"{site_id}: API failed for {product_code}"))
                continue

            site_checked += 1
            total["checked"] += 1
            time.sleep(1.1)

            # Get displayed price from HTML
            displayed_price, displayed_currency = get_displayed_price(html_path, product_code)

            if displayed_price is None:
                total["no_price_found"] += 1
                site_no_price += 1
                continue

            # Normalize to USD (exactly mirrors old price_check.py logic:
            # skips cross-currency when rate unavailable; tracks currency_mismatch)
            if api_currency == displayed_currency:
                if api_currency == "USD":
                    api_usd = api_price
                    displayed_usd = displayed_price
                elif eur_rate:
                    api_usd = api_price * eur_rate
                    displayed_usd = displayed_price * eur_rate
                else:
                    api_usd = api_price
                    displayed_usd = displayed_price
            else:
                total["currency_mismatch"] += 1
                if displayed_currency == "USD" and api_currency == "EUR" and eur_rate:
                    api_usd = normalize_to_usd(api_price, "EUR", eur_rate)
                    displayed_usd = displayed_price
                elif displayed_currency == "EUR" and api_currency == "USD":
                    api_usd = api_price
                    displayed_usd = normalize_to_usd(displayed_price, "EUR", eur_rate)
                else:
                    continue

            if api_usd is None or displayed_usd is None:
                continue

            diff = abs(api_usd - displayed_usd)
            if diff > TOLERANCE_USD:
                total["mismatches"] += 1
                site_mismatches += 1

                api_str = f"${api_price:.2f}" if api_currency == "USD" else f"€{api_price:.2f} (≈${api_usd:.2f})"
                disp_str = f"${displayed_price:.2f}" if displayed_currency == "USD" else f"€{displayed_price:.2f} (≈${displayed_usd:.2f})"

                # Auto-fix for high-confidence cases
                if do_fix and displayed_usd > 0 and api_usd > 0:
                    ratio = displayed_usd / api_usd
                    if abs(1.0 - ratio) > PRICE_FIX_THRESHOLD:
                        result = get_displayed_price(html_path, product_code, return_match=True)
                        if result and len(result) == 4 and result[0] is not None:
                            _, _, match_start, match_end = result
                            new_display_price = (
                                api_usd if displayed_currency == "USD"
                                else (api_price if api_currency == displayed_currency
                                      else round(api_usd / eur_rate, 2) if eur_rate else api_usd)
                            )
                            symbol = "$" if displayed_currency == "USD" else "€"
                            if fix_price_in_html(html_path, match_start, match_end, new_display_price, symbol):
                                total["fixed"] += 1
                                site_fixed_local += 1
                                if not args.quiet:
                                    print(f"  ✅ FIXED: {product_code} — {disp_str} → {api_str}", file=sys.stderr)
                                continue

                total["issues"].append(("drift",
                    f"{site_id}: {product_code} — API {api_str} vs displayed {disp_str} (Δ${diff:+.2f})"))
                if not args.quiet:
                    print(f"  PRICE DRIFT: {site_id}: {product_code} — API {api_str} vs displayed {disp_str} (Δ${diff:+.2f})", file=sys.stderr)

        # Write audit log
        details = (f"{site_checked} checked, {site_mismatches} mismatches, "
                   f"{site_stale} stale, {site_no_price} no-price, "
                   f"{site_skipped_inline} skipped-inline"
                   + (f", {site_fixed_local} fixed" if site_fixed_local else ""))
        write_audit_log(site_id, "price_compare",
                        details=details,
                        issues_found=site_mismatches,
                        status="ok" if site_mismatches == 0 else "issues")
        update_registry_timestamp(site_id, "last_price_check_at")

        if not args.quiet:
            print(f"  {site_id}: {len(pages)} products, {site_skipped_inline} skipped-inline, "
                  f"{site_mismatches} drifts{', ' + str(site_fixed_local) + ' fixed' if site_fixed_local else ''}, "
                  f"{site_stale} stale, {site_no_price} no-price\n", file=sys.stderr)

    # Final summary
    if args.json or is_piped():
        print(json.dumps({
            "checked": total["checked"],
            "mismatches": total["mismatches"],
            "fixed": total["fixed"],
            "stale": total["stale_products"],
            "no_price": total["no_price_found"],
            "api_failures": total["api_failures"],
            "currency_mismatches": total["currency_mismatch"],
            "issues": [{"type": t, "detail": d} for t, d in total["issues"]]
        }))
    elif args.compact:
        print(f"checked={total['checked']} mismatches={total['mismatches']} "
              f"fixed={total['fixed']} stale={total['stale_products']} "
              f"no_price={total['no_price_found']}")
    else:
        print(f"Price Compare complete:")
        print(f"  Products checked:     {total['checked']}")
        print(f"  Price mismatches:     {total['mismatches']}")
        if do_fix:
            print(f"  Auto-fixed:           {total['fixed']}")
        print(f"  Stale (404):           {total['stale_products']}")
        print(f"  No price on page:      {total['no_price_found']}")
        print(f"  API failures:          {total['api_failures']}")
        print(f"  Cross-currency:        {total['currency_mismatch']}")
        if total["issues"]:
            print(f"\nAll issues:")
            for issue_type, issue_text in total["issues"]:
                tag = "⚠️" if issue_type == "drift" else ("🗑️" if issue_type == "stale" else "❌")
                print(f"  {tag} {issue_text}")

    unfixed = total["mismatches"] - total["fixed"]
    sys.exit(EXIT_FOUND if (unfixed > 0 or total["api_failures"] > 0) else EXIT_OK)

# ── Health ──────────────────────────────────────────────────────────────────
def cmd_health(args):
    """Fleet health dashboard."""
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
    gone = db.execute("SELECT COUNT(*) FROM products WHERE active=0").fetchone()[0]
    available = db.execute("SELECT COUNT(*) FROM products WHERE active=1 AND is_available=1").fetchone()[0]
    unavailable = db.execute("SELECT COUNT(*) FROM products WHERE active=1 AND is_available=0").fetchone()[0]
    stale = db.execute(
        "SELECT COUNT(*) FROM products WHERE active=1 AND last_synced < datetime('now', '-7 days')"
    ).fetchone()[0]
    drifts = db.execute("""
        SELECT COUNT(*) FROM (
            SELECT a.product_code FROM prices a
            JOIN prices b ON a.product_code = b.product_code
                AND b.id = (SELECT MAX(id) FROM prices WHERE product_code = a.product_code)
                AND a.id = (SELECT MAX(id) FROM prices WHERE product_code = a.product_code AND id < b.id)
            WHERE a.usd_amount > 0 AND b.usd_amount > 0
                AND ABS((a.usd_amount - b.usd_amount) / a.usd_amount * 100) >= 10
        )
    """).fetchone()[0]
    site_counts = db.execute(
        "SELECT site_id, COUNT(*) cnt FROM site_products GROUP BY site_id ORDER BY cnt DESC"
    ).fetchall()
    last_sync = db.execute(
        "SELECT product_code, last_synced FROM products ORDER BY last_synced DESC LIMIT 1"
    ).fetchone()

    if args.json or is_piped():
        print(json.dumps({
            "products": {
                "active": total, "available": available,
                "unavailable": unavailable, "gone": gone, "stale": stale
            },
            "drifts_significant": drifts,
            "last_sync": last_sync["last_synced"] if last_sync else None,
            "sites": [{"id": s["site_id"], "products": s["cnt"]} for s in site_counts]
        }, default=str))
    elif args.compact:
        print(f"active={total} avail={available} unavail={unavailable} gone={gone} stale={stale} drifts={drifts}")
    else:
        print(f"  Products:    {total} active ({available} available, {unavailable} unavailable, {gone} gone)")
        print(f"  Stale:       {stale} (>7d since last sync)")
        print(f"  Drifts:      {drifts} significant (≥10%)")
        print(f"  Last sync:   {last_sync['last_synced'][:19] if last_sync else 'never'}")
        print(f"  Sites:")
        for s in site_counts:
            print(f"    {s['site_id']:30s}: {s['cnt']} products")

    has_issues = (stale > 0 or drifts > 0 or unavailable > 0)
    sys.exit(EXIT_FOUND if has_issues else EXIT_OK)

# ── SQL ─────────────────────────────────────────────────────────────────────

IN_CLAUSE_MAX_VALUES = 500  # Warn when IN clause exceeds this many values


def _count_in_clause_values(query: str) -> int:
    """Count the max number of values in any IN (...) clause.

    Handles single-quoted strings and parenthesized subqueries heuristically.
    Returns 0 if no IN clause found or count cannot be determined.
    """
    max_count = 0
    # Find IN (...) patterns — handles IN, NOT IN, = ANY(...)-style
    for m in re.finditer(r'\bIN\s*\(', query, re.IGNORECASE):
        paren_start = m.end() - 1  # position of '('
        depth = 0
        i = paren_start
        while i < len(query):
            if query[i] == '(':
                depth += 1
            elif query[i] == ')':
                depth -= 1
                if depth == 0:
                    inside = query[paren_start + 1:i]
                    # Count commas, but not inside nested parens or string literals
                    simple_comma = inside.count(',')
                    # Subtract commas inside nested parens (subqueries)
                    nested = 0
                    sub_depth = 0
                    for ch in inside:
                        if ch == '(':
                            sub_depth += 1
                        elif ch == ')':
                            sub_depth -= 1
                        elif ch == ',' and sub_depth > 0:
                            nested += 1
                    # Subtract commas inside string literals
                    literal_comma = 0
                    in_string = False
                    for ch in inside:
                        if ch == "'" and not in_string:
                            in_string = True
                        elif ch == "'" and in_string:
                            in_string = False
                        elif ch == ',' and in_string:
                            literal_comma += 1
                    val_count = simple_comma - nested - literal_comma + 1  # n commas = n+1 values
                    max_count = max(max_count, val_count)
                    break
            i += 1
    return max_count


def cmd_sql(args):
    """Raw SQL against local mirror. Read-only enforced, with temp-table support."""
    query_original = args.query.strip()
    query_upper = query_original.strip().upper()

    # Gate: allow SELECT, EXPLAIN, PRAGMA, CREATE TABLE _xxx (scratch tables),
    # INSERT INTO _xxx, DROP TABLE IF EXISTS _xxx
    # Underscore-prefixed tables are user scratch tables (persist across connections)
    scratch_table_match = re.match(
        r'(CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+|DROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+)(_\w+)',
        query_original, re.IGNORECASE
    )
    is_scratch_ddl = scratch_table_match is not None

    # Also allow INSERT into _xxx scratch tables
    temp_insert_match = re.match(r'INSERT\s+INTO\s+(_\w+)\b', query_original, re.IGNORECASE)
    is_temp_insert_safe = temp_insert_match is not None and len(query_original) < 100000

    is_select_like = (
        query_upper.startswith("SELECT")
        or query_upper.startswith("EXPLAIN")
        or query_upper.startswith("PRAGMA")
        or query_upper.startswith("WITH")
    )

    if not is_select_like and not is_scratch_ddl and not is_temp_insert_safe:
        print("Error: only SELECT/EXPLAIN/PRAGMA/WITH queries and scratch-table DDL allowed"
              " (read-only enforcement)", file=sys.stderr)
        print("  For large IN clauses, create a scratch table:", file=sys.stderr)
        print("    viator-cli sql \"CREATE TABLE _codes (code TEXT);\"", file=sys.stderr)
        print("    viator-cli sql \"INSERT INTO _codes VALUES ('code1'),('code2'),...;\"", file=sys.stderr)
        print("    viator-cli sql \"SELECT p.* FROM products p JOIN _codes c ON p.product_code=c.code;\"", file=sys.stderr)
        print("    viator-cli sql \"DROP TABLE IF EXISTS _codes;\"  # cleanup", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    # Validate IN clause size for SELECT-like queries
    if is_select_like:
        in_count = _count_in_clause_values(query_original)
        if in_count > IN_CLAUSE_MAX_VALUES:
            print(f"Error: IN clause contains ~{in_count} values (max recommended: {IN_CLAUSE_MAX_VALUES})."
                  f" Large IN clauses can silently truncate or fail.", file=sys.stderr)
            print(f"  Workaround: create a temp table instead:", file=sys.stderr)
            print(f"    viator-cli sql \"CREATE TEMP TABLE _codes (code TEXT);\"", file=sys.stderr)
            print(f"    viator-cli sql \"INSERT INTO _codes VALUES ('code1'),('code2'),...;\"", file=sys.stderr)
            print(f"    viator-cli sql \"SELECT p.* FROM products p JOIN _codes c ON p.product_code = c.code;\"",
                  file=sys.stderr)
            sys.exit(EXIT_USAGE)

    # Check total query size
    if len(query_original) > 100_000:
        print(f"Warning: query is {len(query_original)} chars — very large SQL may cause issues.",
              file=sys.stderr)
        print("  Consider using a temp table approach instead.", file=sys.stderr)

    db = get_db()
    try:
        # Handle scratch table DDL (CREATE/DROP _xxx) — real tables, persist across connections
        if is_scratch_ddl:
            table_name = scratch_table_match.group(2)
            is_create = query_upper.startswith("CREATE")
            db.execute(query_original)
            db.commit()
            action = "Created" if is_create else "Dropped"
            print(f"{action} scratch table `{table_name}` in main DB.", file=sys.stderr)
            sys.exit(EXIT_OK)
        if is_temp_insert_safe:
            db.execute(query_original)
            db.commit()
            print("Inserted.", file=sys.stderr)
            sys.exit(EXIT_OK)

        rows = db.execute(query_original).fetchall()
        for r in rows:
            if args.json or is_piped():
                print(json.dumps(dict(r), default=str))
            elif args.compact:
                vals = [str(v) for v in dict(r).values()]
                print("\t".join(vals))
            else:
                print("\t".join(str(v) for v in dict(r).values()))
        if rows:
            print(f"\n{len(rows)} row(s)", file=sys.stderr)
    except Exception as e:
        print(f"SQL error: {e}", file=sys.stderr)
        sys.exit(EXIT_USAGE)

# ── Doctor ──────────────────────────────────────────────────────────────────
def cmd_doctor(args):
    """Self-diagnostic: DB health, auth, product state, registry integration."""
    issues = []

    # DB file
    if not os.path.exists(DB_PATH):
        issues.append("DB file missing: run 'viator-cli sync' first")
    else:
        try:
            db = sqlite3.connect(DB_PATH)
            total = db.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
            prices_count = db.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            fts_count = db.execute("SELECT COUNT(*) FROM products_fts").fetchone()[0]
            site_count = db.execute("SELECT COUNT(*) FROM site_products").fetchone()[0]
            available = db.execute("SELECT COUNT(*) FROM products WHERE active=1 AND is_available=1").fetchone()[0]
            gone_count = db.execute("SELECT COUNT(*) FROM products WHERE active=0").fetchone()[0]
            db.close()
            if total == 0:
                issues.append("No products synced — run 'viator-cli sync'")
            if prices_count == 0:
                issues.append("No price data — run 'viator-cli sync'")
        except Exception as e:
            issues.append(f"DB error: {e}")

    # API key
    key = get_viator_key()
    if not key:
        issues.append("VIATOR_API_KEY not set — sync will fail")
    else:
        if len(key) < 20:
            issues.append(f"VIATOR_API_KEY looks truncated ({len(key)} chars)")

    # Registry
    if not os.path.exists(REGISTRY_PATH):
        issues.append("site_registry.db not found — sync has no sites to scan")
    else:
        try:
            reg = sqlite3.connect(REGISTRY_PATH)
            active_sites = reg.execute("SELECT COUNT(*) FROM sites WHERE status='active'").fetchone()[0]
            reg.close()
            if active_sites == 0:
                issues.append("No active sites in registry")
        except Exception as e:
            issues.append(f"Registry error: {e}")

    # Output
    if args.json or is_piped():
        print(json.dumps({
            "healthy": len(issues) == 0,
            "db_path": DB_PATH,
            "issues": issues,
            "products": total if 'total' in dir() else 0,
            "prices": prices_count if 'prices_count' in dir() else 0,
            "sites": site_count if 'site_count' in dir() else 0,
            "available": available if 'available' in dir() else 0,
            "gone": gone_count if 'gone_count' in dir() else 0,
        }))
    else:
        if issues:
            print(f"Found {len(issues)} issue(s):")
            for i in issues:
                print(f"  • {i}")
        else:
            print("✓ All checks passed")
            print(f"  DB:         {DB_PATH}")
            print(f"  Products:   {total} active ({available} available, {gone_count} gone)" if 'available' in dir() else f"  Products:   0")
            print(f"  Prices:     {prices_count} records" if 'prices_count' in dir() else "  Prices:     0 records")
            print(f"  Site maps:  {site_count} mappings" if 'site_count' in dir() else "  Site maps:  0 mappings")
            print(f"  Registry:   {REGISTRY_PATH}")

    sys.exit(EXIT_FOUND if issues else EXIT_OK)


def cmd_validate(args):
    """Validate whether a string is a real product code in the local mirror.

    Exit codes:
      0 = valid product code found in DB
      1 = not a product code (not found or empty string)
      2 = fallback/category code (ttd, g5335, all — do not use)

    With --json, returns {"valid": bool, "product_code": str, "title": str|null, "reason": str|null}.
    """
    db = get_db()
    code = args.code.strip()
    FALLBACK_CODES = {'ttd', 'ttd-', 'g5335', 'all', 'ttd-'}

    if not code:
        if args.json:
            print(json.dumps({"valid": False, "product_code": "", "title": None,
                              "reason": "empty string"}))
        else:
            print(f"\u2717  (empty) — not a product code")
        sys.exit(EXIT_FOUND)

    code_lower = code.lower()
    if code_lower in FALLBACK_CODES or code_lower.startswith('ttd'):
        if args.json:
            print(json.dumps({"valid": False, "product_code": code, "title": None,
                              "reason": "fallback category code (do not use)"}))
        else:
            print(f"\u26a0 {code} — category fallback code (do not use)")
        sys.exit(2)

    row = db.execute(
        "SELECT product_code, title FROM products WHERE product_code=?",
        (code,)
    ).fetchone()

    if row:
        if args.json:
            print(json.dumps({"valid": True, "product_code": row['product_code'],
                              "title": row['title'], "reason": None}))
        else:
            print(f"\u2713 {row['product_code']} — {row['title']}")
        sys.exit(EXIT_OK)

    # Not found in DB
    if args.json:
        print(json.dumps({"valid": False, "product_code": code, "title": None,
                          "reason": "not a product code"}))
    else:
        print(f"\u2717 {code} — not a product code")
    sys.exit(EXIT_FOUND)


# ── CLI ─────────────────────────────────────────────────────────────────────
def cmd_search(args):
    """Live freetext search — for freetext API profiles."""
    db = get_db()
    term = args.search_term
    dest_id = getattr(args, "dest_id", None)
    limit = args.limit
    site_id = getattr(args, "site", None)

    all_results = []
    start = 1
    while len(all_results) < limit:
        data, err = viator_freetext_search(term, dest_id=dest_id, start=start, count=min(50, limit))
        if err:
            print(f"Search error: {err}", file=sys.stderr)
            sys.exit(EXIT_API)
        prods = data.get("products", {})
        results = prods.get("results", [])
        total = prods.get("totalCount", 0)
        all_results.extend(results)
        if start + 49 >= total or len(results) < 50:
            break
        start += 50
        time.sleep(0.05)

    if args.sync:
        stored = 0
        for prod in all_results[:limit]:
            r = sync_product_from_freetext(db, prod, site_id=site_id)
            if r:
                stored += 1
        db.commit()
        db.close()
        print(f"Stored {stored} products", file=sys.stderr)

    if args.json:
        print(json.dumps(all_results[:limit], indent=2))
    elif args.compact:
        for p in all_results[:limit]:
            code = p.get("productCode", "")
            title = p.get("title", "")[:60]
            print(f"{code}  {title}")
    else:
        for i, p in enumerate(all_results[:limit], 1):
            code = p.get("productCode", "")
            title = p.get("title", "")
            price = p.get("pricing", {}).get("summary", {}).get("fromPrice", "")
            rating = (p.get("reviews") or {}).get("combinedAverageRating", "")
            reviews = (p.get("reviews") or {}).get("totalReviews", 0)
            dests = p.get("destinations", [])
            dest = dests[0].get("ref", "") if dests else ""
            print(f"{i:3d}. {code:15s}  ${price if price else '?'}")
            print(f"      {title[:80]}")
            if rating:
                print(f"      ★{rating} ({reviews} reviews)  dest={dest}")
            print()

    sys.exit(EXIT_OK)


# ── Bulk Sync ───────────────────────────────────────────────────────────────
def cmd_bulk_sync(args):
    """Bulk-sync all 8 Hanumanhermes sites via freetext search.

    For each site, iterates through its configured queries (destId-based or
    text-only), paginates through freetext results, and stores products with
    site_products mapping.

    Rate limiting: 50ms between pages, 2s between destId changes.
    """
    dry_run = getattr(args, "dry_run", False)
    site_filter = getattr(args, "site", None)

    sites_to_sync = {}
    if site_filter:
        if site_filter not in HANUMANHERMES_SITES:
            print(f"Unknown site: {site_filter}", file=sys.stderr)
            print(f"Known sites: {', '.join(sorted(HANUMANHERMES_SITES.keys()))}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
        sites_to_sync[site_filter] = HANUMANHERMES_SITES[site_filter]
    else:
        sites_to_sync = dict(HANUMANHERMES_SITES)

    if not dry_run:
        db = get_db()
    else:
        db = None

    global_totals = {"sites": 0, "products": 0, "new": 0, "updated": 0, "errors": 0, "queries": 0, "pages": 0}

    for site_id, site_cfg in sorted(sites_to_sync.items()):
        if not dry_run:
            print(f"\n{'='*60}", file=sys.stderr)
            print(f"  SITE: {site_id} ({site_cfg['domain']})", file=sys.stderr)
            print(f"  {site_cfg['description']}", file=sys.stderr)
            print(f"{'='*60}", file=sys.stderr)

        site_products = 0
        site_new = 0
        site_updated = 0
        site_errors = 0
        site_skipped_dup = 0

        for q_idx, query in enumerate(site_cfg["queries"]):
            term = query["search_term"]
            dest_id = query.get("dest_id")
            dest_name = query.get("dest_name", term)
            query_products = 0
            query_pages = 0

            if dry_run:
                # Estimate counts with a single API call
                data, err = viator_freetext_search(term, dest_id=dest_id, start=1, count=1)
                if err:
                    if args.verbose:
                        print(f"  [!] {dest_name}: {err}", file=sys.stderr)
                    continue
                prods_obj = data.get("products", {})
                total_est = prods_obj.get("totalCount", 0)
                est_pages = (total_est + 49) // 50 if total_est else 0
                print(f"  {dest_name:30s}  ~{total_est} products ({est_pages} pages)")
                continue

            # Full paginated fetch
            start = 1
            page_size = 50
            while True:
                data, err = viator_freetext_search(term, dest_id=dest_id, start=start, count=page_size)
                if err:
                    print(f"  [!] {dest_name} page {start}: {err}", file=sys.stderr)
                    site_errors += 1
                    global_totals["errors"] += 1
                    break

                prods_obj = data.get("products", {})
                results = prods_obj.get("results", [])
                total_count = prods_obj.get("totalCount", 0)

                if not results:
                    break

                for prod in results:
                    code = prod.get("productCode", "")
                    # Track new vs updated: check site_products mapping for this site
                    existing = False
                    if db:
                        existing = db.execute(
                            "SELECT 1 FROM site_products WHERE site_id=? AND product_code=?",
                            (site_id, code)
                        ).fetchone() is not None

                    result = sync_product_from_freetext(db, prod, site_id=site_id)
                    if result:
                        query_products += 1
                        site_products += 1
                        global_totals["products"] += 1
                        if existing:
                            site_updated += 1
                            global_totals["updated"] += 1
                        else:
                            site_new += 1
                            global_totals["new"] += 1
                    else:
                        site_errors += 1
                        global_totals["errors"] += 1

                query_pages += 1
                global_totals["pages"] += 1

                # Pagination: check if we've hit the last page
                current_end = start + len(results) - 1
                if current_end >= total_count:
                    break
                start += page_size
                time.sleep(0.05)  # 50ms between pages

            global_totals["queries"] += 1
            if not dry_run:
                print(f"  [{q_idx+1}/{len(site_cfg['queries'])}] {dest_name:30s}  {query_products} products ({query_pages} pages)", file=sys.stderr)

            # 2s between destIds (but not after the last query of a site)
            if q_idx < len(site_cfg["queries"]) - 1:
                time.sleep(2.0)

        if not dry_run:
            site_new_str = f", {site_new} new" if site_new else ""
            site_upd_str = f", {site_updated} updated" if site_updated else ""
            site_err_str = f", {site_errors} errors" if site_errors else ""
            print(f"  ── {site_id}: {site_products} products synced{site_new_str}{site_upd_str}{site_err_str}", file=sys.stderr)

            # Commit after each site
            db.commit()

        global_totals["sites"] += 1

    if dry_run:
        total_est = 0
        print(f"\n[Dry-run complete]", file=sys.stderr)
        print(f"[DRY-RUN] Would sync products via {len(sites_to_sync)} site(s), {sum(len(c['queries']) for c in sites_to_sync.values())} queries", file=sys.stderr)
        print(f"[DRY-RUN] Per-site estimates shown above", file=sys.stderr)
        print(f"[DRY-RUN] Would store: product_code, title, description, destination info, price, site_products mapping", file=sys.stderr)
        sys.exit(EXIT_OK)

    # Final commit + checkpoint
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

    new_str = f"new={global_totals['new']} " if global_totals['new'] else ""
    upd_str = f"updated={global_totals['updated']} " if global_totals['updated'] else ""
    err_str = f"errors={global_totals['errors']} " if global_totals['errors'] else ""
    print(f"\nbulk-sync: {global_totals['products']} products across {global_totals['sites']} sites ({new_str}{upd_str}{err_str}{global_totals['pages']} pages, {global_totals['queries']} queries)", file=sys.stderr)
    db.close()
    sys.exit(EXIT_FOUND if global_totals['errors'] > 0 else EXIT_OK)


def main():
    parser = argparse.ArgumentParser(
        description="viator-cli — agent-native Viator CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  viator-cli sync --dry-run
  viator-cli sync --verbose
  viator-cli catalog --dry-run        # Estimate catalog size
  viator-cli catalog --count=100      # Sync first 100 products from each destination
  viator-cli catalog                   # Full catalog sync
  viator-cli products --site porto-wine-tours
  viator-cli products --search wine --compact  # 80% fewer tokens
  viator-cli product 12546P1 --prices --json
  viator-cli prices --min-pct 15 --site porto
  viator-cli compare --site porto-wine-tours
  viator-cli compare --fix  # auto-fix clear mispricing
  viator-cli health
  viator-cli sql "SELECT title FROM products WHERE rating >= 4.5"
  viator-cli doctor

  viator-cli --profile hanumanhermes bulk-sync [--site onsenexperiences] [--dry-run]
      Bulk-sync all 8 Hanumanhermes sites via freetext search. Paginated,
      rate-limited, per-site reporting with new/updated tracking. Supports
      --dry-run for estimation and --site for single-site operation.
""")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Progress output to stderr")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress stderr output")
    parser.add_argument("--profile", help="Agent profile: hanumanhermes (default: Saraswati fleet)")

    sub = parser.add_subparsers(dest="command")

    # catalog — full enumeration
    p = sub.add_parser("catalog", help="Full catalog sync from /products/search per destination")
    p.add_argument("--dry-run", action="store_true", help="Estimate product counts without storing")
    p.add_argument("--count", type=int, default=0, help="Max products per destination (0 = all)")
    p.add_argument("--verbose", "-v", action="store_true", help="Per-destination progress output")

    # sync
    p = sub.add_parser("sync", help="Sync product catalog from Viator API")
    p.add_argument("--site", default="all")
    p.add_argument("--full", action="store_true")
    p.add_argument("--discover", action="store_true", help="Discover codes from HTML files instead of registry")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true", help="Progress output")
    p.add_argument("--quiet", "-q", action="store_true", help="Suppress stderr")

    # products
    p = sub.add_parser("products", help="Query local mirror (no API calls)")
    p.add_argument("--site", help="Filter to one site's products (e.g. onsenexperiences). Combines with --search for site-scoped FTS5 ranking.")
    p.add_argument("--search", help="FTS5 full-text search on title/description. Results ranked by BM25 relevance.")
    p.add_argument("--destination")
    p.add_argument("--price-dropped", type=float, metavar="PCT")
    p.add_argument("--stale", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--compact", action="store_true", help="product_code + title only")
    p.add_argument("--json", action="store_true", help="JSON array of full product objects")
    p.add_argument("--csv", action="store_true", help="Header row + data rows")

    # resolve — FTS5 product code resolution (replaces ttd/g5335 fallbacks)
    p = sub.add_parser("resolve", help="Resolve a region+activity query to a product code via FTS5")
    p.add_argument("query", help="Search query (e.g. 'Beppu onsen tour')")
    p.add_argument("--dest-id", help="Destination ID filter (prevents wrong-region results)")
    p.add_argument("--compact", action="store_true", help="Shorter output")
    p.add_argument("--json", action="store_true", help="JSON output")

    # product
    p = sub.add_parser("product", help="One product's details with canonical URL")
    p.add_argument("code")
    p.add_argument("--prices", action="store_true")
    p.add_argument("--compact", action="store_true")
    p.add_argument("--json", action="store_true")

    # prices
    p = sub.add_parser("prices", help="Price drift report (DB internal)")
    p.add_argument("--site")
    p.add_argument("--min-pct", type=float, default=5.0)
    p.add_argument("--limit", type=int)
    p.add_argument("--compact", action="store_true")
    p.add_argument("--json", action="store_true")

    # compare — HTML vs API price comparison (from price_check.py)
    p = sub.add_parser("compare", help="HTML-vs-API price comparison (every site, every product card)")
    p.add_argument("--site", help="Filter to one site (default: all)")
    p.add_argument("--fix", action="store_true", help="Auto-replace clearly wrong prices (displayed less than 50%% of API)")
    p.add_argument("--compact", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", "-q", action="store_true", help="Suppress per-drift output")

    # health
    p = sub.add_parser("health", help="Fleet health dashboard")
    p.add_argument("--compact", action="store_true")
    p.add_argument("--json", action="store_true")

    # sql
    p = sub.add_parser("sql", help="Raw SQL against local mirror (read-only)")
    p.add_argument("query", help="SQL SELECT/EXPLAIN/PRAGMA query")
    p.add_argument("--compact", action="store_true")
    p.add_argument("--json", action="store_true")

    # doctor
    p = sub.add_parser("doctor", help="Self-diagnostic: DB, auth, product health")
    p.add_argument("--json", action="store_true")

    # validate — check if string is a real product code
    p = sub.add_parser("validate", help="Check if a string is a real product code (exit: 0=valid, 1=invalid, 2=fallback)")
    p.add_argument("code", help="Product code to validate (e.g. 5581898P7)")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # errors
    p = sub.add_parser("errors", help="Recent sync errors from Viator API")
    p.add_argument("--limit", type=int, default=20, help="Max errors to show (default: 20)")
    p.add_argument("--since", help="ISO date filter (e.g. 2026-06-28)")

    # search — freetext search (for hanumanhermes and other freetext profiles)
    p = sub.add_parser("search", help="Live freetext search via /partner/search/freetext")
    p.add_argument("search_term", help="Search term (e.g. 'wine tasting')")
    p.add_argument("--dest-id", help="Destination ID filter")
    p.add_argument("--site", help="Site ID for site_products mapping")
    p.add_argument("--limit", type=int, default=10, help="Max results to return")
    p.add_argument("--sync", action="store_true", help="Store results in DB")
    p.add_argument("--compact", action="store_true")
    p.add_argument("--json", action="store_true")

    # bulk-sync — bulk sync all Hanumanhermes sites via freetext
    p = sub.add_parser("bulk-sync", help="Bulk-sync all 8 Hanumanhermes sites via freetext search")
    p.add_argument("--site", help="Sync only one site (e.g. onsenexperiences)")
    p.add_argument("--dry-run", action="store_true", help="Estimate counts without storing")
    p.add_argument("--verbose", "-v", action="store_true", help="Detailed progress output")

    args = parser.parse_args()

    # Apply profile BEFORE any DB/API operations
    profile = getattr(args, "profile", None)
    resolve_profile(profile)

    if not args.command:
        parser.print_help()
        sys.exit(EXIT_OK)

    {
        "catalog": cmd_sync_catalog,
        "sync": cmd_sync,
        "products": cmd_products,
        "resolve": cmd_resolve,
        "product": cmd_product,
        "prices": cmd_prices,
        "compare": cmd_compare,
        "health": cmd_health,
        "sql": cmd_sql,
        "doctor": cmd_doctor,
        "validate": cmd_validate,
        "errors": cmd_errors,
        "search": cmd_search,
        "bulk-sync": cmd_bulk_sync,
    }[args.command](args)

if __name__ == "__main__":
    main()
