#!/usr/bin/env python3
"""
product_registry.py — Build canonical products.json from Viator API for one site.

Reads a Hanumanhermes content bank YAML, fetches live product data from the
Viator Partner API, and produces a products.json keyed by product_code.

Output schema (per product_code):
  {
    "title": str,
    "canonical_url": str (with pid/mcid partner params),
    "rating": float | null,
    "review_count": int,
    "price_band": "$" | "$$" | "$$$" | "$$$$" | "Unknown",
    "destination_name": str
  }

Usage:
  python3 product_registry.py \\
    --site reefandrod \\
    --content-bank ~/.hermes/profiles/hanumanhermes/data/content-banks/reefandrod.yaml \\
    --output ~/.hermes/profiles/hanumanhermes/data/registries/reefandrod/products.json

Exit codes:
  0 — success, registry written
  1 — API auth failure
  3 — usage error
  4 — registry exists but no valid API key (auth guard)
  5 — API errors encountered (exceptions written, partial registry OK)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

import yaml

VERSION = "1.0.0"

# ── Exit codes ──────────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_AUTH = 1
EXIT_USAGE = 3
EXIT_GUARD = 4
EXIT_API = 5

# ── Partner params for canonical URLs ───────────────────────────────────
PARTNER_ID = "P00299531"
MCID = "42383"

# ── API ─────────────────────────────────────────────────────────────────

def get_api_key() -> str:
    """Read Viator API key from environment or ~/.hermes/.env."""
    key = os.environ.get("VIATOR_API_KEY", "")
    if key:
        return key
    env_paths = [
        os.path.expanduser("~/.hermes/.env"),
        os.path.expanduser("~/.hermes/profiles/hanumanhermes/.env"),
    ]
    for env_path in env_paths:
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("VIATOR_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if key:
                            return key
    return ""


def fetch_product(code: str, api_key: str, cache: Optional[dict] = None):
    """Fetch product detail from Viator Partner API.

    Endpoint: GET /partner/products/{code}

    Args:
        code: Viator product code (e.g. "26254P67")
        api_key: Viator API key (exp-api-key header)
        cache: Optional dict for dedup (successes cached; failures never cached)

    Returns:
        (data_dict, None) on success
        (None, error_string) on failure
    """
    if cache is not None and code in cache:
        cached = cache[code]
        if cached is None:
            return None, "CACHED_NULL"
        return cached, None

    url = f"https://api.viator.com/partner/products/{code}"
    data, error = _viator_get(url, api_key)
    if data is not None and cache is not None:
        cache[code] = data
    return data, error


def fetch_pricing(code: str, api_key: str) -> Optional[float]:
    """Fetch pricing from availability/schedules endpoint.

    Endpoint: GET /partner/availability/schedules/{code}
    Returns from_price (float) or None on failure.
    """
    url = f"https://api.viator.com/partner/availability/schedules/{code}"
    data, error = _viator_get(url, api_key)
    if error or not data:
        return None
    summary = data.get("summary", {}) or {}
    return summary.get("fromPrice")


def _viator_get(url: str, api_key: str):
    """Low-level Viator GET with retry/backoff.

    Returns (data_dict, None) or (None, error_string).
    """
    headers = {
        "exp-api-key": api_key,
        "Accept": "application/json;version=2.0",
        "Accept-Language": "en",
    }

    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read()), None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt < 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                return None, f"HTTP 429: rate limited"
            if e.code == 404:
                return None, f"HTTP 404: product not found"
            if e.code in (401, 403):
                return None, f"HTTP {e.code}: auth failure — check API key"
            if e.code == 400:
                return None, f"HTTP 400: bad request"
            if attempt < 1:
                time.sleep(2)
                continue
            return None, f"HTTP {e.code}: {e.reason}"
        except Exception as e:
            if attempt < 1:
                time.sleep(2)
                continue
            return None, str(e)
    return None, "max retries exceeded"


# ── Content Bank Parser ─────────────────────────────────────────────────

def parse_content_bank(yaml_path: str) -> list[dict]:
    """Parse a Hanumanhermes content bank YAML and extract product entries.

    Returns list of dicts: {code, label, destId, content_url}
    """
    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    products = data.get("products", [])
    if not products:
        return []

    result = []
    for p in products:
        code = p.get("viator_code", "")
        if not code:
            continue
        result.append({
            "code": code,
            "label": p.get("title", ""),
            "destId": p.get("destId"),
            "content_url": p.get("viator_url", ""),
        })
    return result


# ── Formatters ──────────────────────────────────────────────────────────

def format_duration(dur: Optional[dict]) -> str:
    """Format Viator duration dict to human-readable string."""
    if not dur:
        return ""
    if "fixedDurationInMinutes" in dur:
        mins = dur["fixedDurationInMinutes"]
        h, m = divmod(mins, 60)
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"
    if "variableDurationFromMinutes" in dur:
        lo = dur["variableDurationFromMinutes"]
        hi = dur["variableDurationToMinutes"]
        h_lo, m_lo = divmod(lo, 60)
        h_hi, m_hi = divmod(hi, 60)
        return f"{h_lo}h {m_lo}m–{h_hi}h {m_hi}m"
    return ""


def price_band(amount: Optional[float]) -> str:
    """Map USD price to price band symbol."""
    if amount is None:
        return "Unknown"
    if amount < 50:
        return "$"
    if amount < 150:
        return "$$"
    if amount < 400:
        return "$$$"
    return "$$$$"


# ── Title Matching ──────────────────────────────────────────────────────

def titles_match(label: str, api_title: str) -> bool:
    """Check if content bank label reasonably matches API title.

    Normalizes whitespace, case, and punctuation. Returns True if
    the API title appears to be the same product (exact or substring).
    """
    def normalize(s: str) -> str:
        return re.sub(r"\s+", " ", s.lower().strip(" '\""))

    nl = normalize(label)
    na = normalize(api_title)
    if not nl or not na:
        return False
    if nl == na:
        return True
    # API title is often shorter/more generic — check if label contains it
    if na in nl or nl in na:
        return True
    return False


# ── Auth Guard ──────────────────────────────────────────────────────────

def auth_guard(registry_path: str, api_key: str) -> bool:
    """Refuse to overwrite existing registry if API key is missing.

    Returns True if safe to proceed, False if blocked.
    """
    if os.path.exists(registry_path) and not api_key:
        print(f"ERROR: {registry_path} exists but VIATOR_API_KEY is not set.", file=sys.stderr)
        print("Refusing to overwrite existing registry without valid auth.", file=sys.stderr)
        return False
    return True


# ── Entry Builder ───────────────────────────────────────────────────────

def build_entry(code: str, data: dict, from_price: Optional[float] = None) -> dict:
    """Build a products.json entry from Viator API response.

    Args:
        code: Product code (key)
        data: Full API response dict
        from_price: Optional price from availability/schedules endpoint

    Returns:
        {title, canonical_url, rating, review_count, price_band, destination_name}
    """
    title = data.get("title") or ""

    # Destination name — API product detail doesn't include names in destinations[].
    # Extract from productUrl path: /tours/{Destination-Name}/{tour-slug}/d{id}-{code}
    dest_name = _extract_dest_from_url(data.get("productUrl") or "")
    if not dest_name:
        # Fallback: check destinations[].name (present in search API responses)
        destinations = data.get("destinations", [])
        for d in destinations:
            if d.get("primary"):
                dest_name = d.get("name", "")
                break
        if not dest_name and destinations:
            dest_name = destinations[0].get("name", "")

    # Reviews
    reviews = data.get("reviews", {}) or {}
    rating = reviews.get("combinedAverageRating")
    review_count = reviews.get("totalReviews", 0) or 0

    # Pricing — from availability/schedules endpoint (preferred) or search API
    if from_price is None:
        from_price = _extract_price(data)
    band = price_band(from_price)

    # Canonical URL — build with partner params
    canonical_url = _build_canonical_url(code, title, data.get("productUrl", ""))

    return {
        "title": title,
        "canonical_url": canonical_url,
        "rating": rating,
        "review_count": review_count,
        "price_band": band,
        "destination_name": dest_name,
    }


def _extract_dest_from_url(product_url: str) -> str:
    """Extract destination name from Viator product URL path.

    Format: https://www.viator.com/tours/{Destination-Name}/{tour-slug}/d{id}-{code}
    Returns 'Jerusalem' from '/tours/Jerusalem/Day-Tour.../d921-CODE'
    Returns '' if URL is empty, None, or missing /tours/ path.
    """
    if not product_url:
        return ""
    try:
        after_tours = product_url.split("/tours/", 1)
        if len(after_tours) < 2:
            return ""
        dest_slug = after_tours[1].split("/")[0]
        if not dest_slug or dest_slug.startswith("d"):
            return ""
        return dest_slug.replace("-", " ").title()
    except (IndexError, AttributeError):
        return ""


def _extract_price(data: dict) -> Optional[float]:
    """Extract fromPrice from various API response shapes.

    Search API: pricing.summary.fromPrice
    Product detail API: pricingInfo doesn't have fromPrice directly.
    """
    # Search API shape
    pricing = data.get("pricing", {}) or {}
    summary = pricing.get("summary", {}) or {}
    price = summary.get("fromPrice")
    if price is not None:
        return price

    # Product detail shape — pricingInfo doesn't have fromPrice;
    # try productOptions pricing (may not be loaded without availability check)
    return None


def _build_canonical_url(code: str, title: str, product_url: str = "") -> str:
    """Build canonical Viator URL with partner params."""
    if product_url:
        # Strip existing params, add ours
        base = product_url.split("?")[0]
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
        base = f"https://www.viator.com/tours/{slug}/{code}"
    
    return f"{base}?pid={PARTNER_ID}&mcid={MCID}&medium=link"


# ── Main Pipeline ───────────────────────────────────────────────────────

def build_registry(
    content_bank_path: str,
    registry_path: str,
    exceptions_path: str,
    api_key: str,
    delay: int = 500,
    limit: Optional[int] = None,
) -> tuple[dict, dict]:
    """Main pipeline: parse content bank → fetch API → build registry.

    Args:
        content_bank_path: Path to content bank YAML
        registry_path: Output path for products.json
        exceptions_path: Output path for exceptions.json (failed products)
        api_key: Viator API key
        delay: Milliseconds between API calls (rate limiting)
        limit: Max products to fetch (None = all)

    Returns:
        (registry dict, exceptions dict)
    """
    # Parse content bank
    products = parse_content_bank(content_bank_path)

    # Ensure output directory exists BEFORE any writes
    os.makedirs(os.path.dirname(registry_path), exist_ok=True)

    if not products:
        print("No products found in content bank. Writing empty registry.")
        registry = {}
        _write_json(registry_path, registry)
        _write_json(exceptions_path, {})
        return registry, {}

    if limit:
        products = products[:limit]

    total = len(products)
    print(f"Found {total} products in content bank. Fetching from Viator API...")
    print(f"Rate limit: {delay}ms between calls (~{total * delay / 1000:.0f}s total)\n")

    registry = {}
    exceptions = {}
    cache = {}

    for i, p in enumerate(products):
        code = p["code"]
        label = p["label"]
        print(f"  [{i+1}/{total}] {code} — {label[:60]}", end="", flush=True)

        data, error = fetch_product(code, api_key, cache)

        if error:
            print(f"  FAIL: {error}")
            exceptions[code] = {
                "label": label,
                "error": error,
            }
        elif data.get("status") == "INACTIVE":
            print(f"  INACTIVE")
            exceptions[code] = {
                "label": label,
                "error": "INACTIVE — product no longer available on Viator",
            }
        else:
            # Fetch pricing from availability endpoint (best-effort)
            from_price = fetch_pricing(code, api_key)
            entry = build_entry(code, data, from_price=from_price)
            # Title match check
            api_title = entry["title"]
            if not titles_match(label, api_title):
                print(f"  MISMATCH: '{label}' → API: '{api_title}'")
                # Record mismatch but still include in registry
            else:
                print(f"  OK: {api_title[:60]}")

            registry[code] = entry

        # Rate limiting
        if i < total - 1 and delay > 0:
            time.sleep(delay / 1000.0)

    # Write outputs
    _write_json(registry_path, registry)
    _write_json(exceptions_path, exceptions)

    print(f"\nDone: {len(registry)} products in registry, {len(exceptions)} exceptions")
    return registry, exceptions


def _write_json(path: str, data: dict) -> None:
    """Write dict as pretty-printed JSON."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    print(f"  Wrote: {path}")


# ── CLI ─────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build canonical products.json from Viator API for one site."
    )
    parser.add_argument("--site", required=True, help="Site slug (e.g. reefandrod)")
    parser.add_argument(
        "--content-bank", required=True,
        help="Path to content bank YAML file",
    )
    parser.add_argument(
        "--output", required=True,
        help="Output path for products.json",
    )
    parser.add_argument(
        "--exceptions", default=None,
        help="Output path for exceptions.json (default: {output_dir}/exceptions.json)",
    )
    parser.add_argument(
        "--delay", type=int, default=500,
        help="Milliseconds between API calls (default: 500)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max products to fetch (default: all)",
    )
    parser.add_argument(
        "--api-key", default=None,
        help="Viator API key (default: read from env or .env file)",
    )
    return parser.parse_args(argv)


def main():
    args = parse_args()

    api_key = args.api_key or get_api_key()

    # Content bank
    cb_path = os.path.expanduser(args.content_bank)
    if not os.path.exists(cb_path):
        print(f"ERROR: Content bank not found: {cb_path}", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    # Output paths
    output_path = os.path.expanduser(args.output)
    exceptions_path = os.path.expanduser(
        args.exceptions or os.path.join(os.path.dirname(output_path), "exceptions.json")
    )

    # Auth guard
    if not auth_guard(output_path, api_key):
        sys.exit(EXIT_GUARD)

    if not api_key:
        print("ERROR: VIATOR_API_KEY not set", file=sys.stderr)
        sys.exit(EXIT_AUTH)

    registry, exceptions = build_registry(
        cb_path, output_path, exceptions_path,
        api_key=api_key, delay=args.delay, limit=args.limit,
    )

    if exceptions:
        print(f"\n{len(exceptions)} product(s) failed — see {exceptions_path}")
        # Partial success is still success unless everything failed
        if len(registry) == 0:
            sys.exit(EXIT_API)

    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
