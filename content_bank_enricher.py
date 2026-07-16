#!/usr/bin/env python3
"""
Content Bank Enricher — auto-replenishes thin product data from Viator API + gbrain.

Trigger: content-drip cron detects quality < threshold after max retries.
Action: queries Viator API for product details, gbrain for niche knowledge,
        updates the content bank YAML so future runs have richer data.

Usage: python3 content_bank_enricher.py <site_slug> [product_code]
  Without product_code: enriches ALL thin products in the content bank.
  With product_code: enriches only the specified product.
"""

import os, sys, json, yaml, re, sqlite3, urllib.request, urllib.error, time
from datetime import datetime, timezone
from pathlib import Path

HERMES_DIR = os.path.expanduser("~/.hermes")
CONTENT_BANKS_DIR = os.path.join(HERMES_DIR, "affiliate-crons", "content-banks")
REGISTRY_PATH = os.path.join(HERMES_DIR, "affiliate-crons", "db", "site_registry.db")
ENV_PATH = os.path.join(HERMES_DIR, ".env")

# What constitutes "thin" — product fields that should be non-empty
REQUIRED_PRODUCT_FIELDS = ["custom_blurb", "best_for"]
RECOMMENDED_PRODUCT_FIELDS = ["rating", "reviews"]

# Minimum enrichment thresholds
MIN_BLURB_CHARS = 50
MIN_FACTS = 5
MIN_STORIES = 3


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


def viator_get(path):
    """Call Viator API. Returns data dict or None."""
    key = get_viator_key()
    if not key:
        return None
    url = f"https://api.viator.com/partner{path}"
    headers = {
        "exp-api-key": key,
        "Accept": "application/json;version=2.0",
        "Accept-Language": "en"
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except Exception:
        return None


def fetch_product_details(code):
    """Fetch full product details from Viator API."""
    data = viator_get(f"/products/{code}")
    if not data:
        return None

    title = data.get("title", "")
    description = data.get("description", "")
    # Strip HTML from description
    if description:
        description = re.sub(r'<[^>]+>', ' ', description)
        description = re.sub(r'\s+', ' ', description).strip()

    duration = data.get("duration", "")
    dest_name = data.get("destinationName", "")
    cat = data.get("category", "")
    subcat = data.get("subCategory", "")
    reviews_data = data.get("reviews", {}) or {}
    rating = reviews_data.get("combinedAverageRating")
    total_reviews = reviews_data.get("totalReviews", 0)

    # Generate a blurb from description if needed
    blurb = ""
    if description:
        # First 1-2 sentences of description make a good blurb
        sentences = re.split(r'(?<=[.!?])\s+', description[:300])
        blurb = ' '.join(sentences[:2]).strip()
        if len(blurb) < MIN_BLURB_CHARS:
            blurb = description[:200].strip()

    # Determine best_for from category/duration
    best_for = "All experience levels"  # default
    if duration:
        hours_match = re.search(r'(\d+)\s*(?:hour|hr|h|stunde)', duration, re.IGNORECASE)
        if hours_match:
            hrs = int(hours_match.group(1))
            if hrs >= 8:
                best_for = "Experienced adventurers with full-day stamina"
            elif hrs >= 4:
                best_for = "Active travelers comfortable with half-day excursions"
            else:
                best_for = "All fitness levels — short and accessible"

    return {
        "title": title,
        "description": description[:500],
        "duration": duration,
        "destination": dest_name,
        "category": f"{cat} > {subcat}" if subcat else cat,
        "rating": rating,
        "reviews": total_reviews,
        "auto_blurb": blurb,
        "auto_best_for": best_for,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def query_gbrain_knowledge(slug, niche, destination):
    """Query gbrain for domain knowledge about the niche/destination."""
    gbrain_dir = os.path.join(HERMES_DIR, "gbrain")
    knowledge_path = os.path.join(gbrain_dir, "knowledge", f"{slug}.json")

    # Try the structured gbrain knowledge first
    if os.path.exists(knowledge_path):
        try:
            with open(knowledge_path) as f:
                gb = json.load(f)
        except Exception:
            gb = {}

        facts = []
        # Extract facts from gbrain entries
        for entry in gb.get("entries", []):
            content = entry.get("content", "")
            if content and len(content) > 20:
                # Take sentence-level facts
                sentences = re.split(r'(?<=[.!?])\s+', content)
                for s in sentences[:5]:
                    s = s.strip()
                    if len(s) > 30 and len(s) < 250:
                        facts.append(s)

        return facts if facts else None

    # Fallback: scan gbrain capture files
    capture_dir = os.path.join(gbrain_dir, "captures", slug)
    if os.path.isdir(capture_dir):
        facts = []
        for fname in sorted(os.listdir(capture_dir))[-3:]:  # last 3 captures
            fpath = os.path.join(capture_dir, fname)
            try:
                with open(fpath) as f:
                    content = f.read()
                sentences = re.split(r'(?<=[.!?])\s+', content)
                for s in sentences:
                    s = s.strip()
                    if len(s) > 30 and len(s) < 250:
                        facts.append(s)
                        if len(facts) >= 10:
                            break
            except Exception:
                continue
        return facts[:10] if facts else None

    return None


def load_content_bank(slug):
    path = os.path.join(CONTENT_BANKS_DIR, f"{slug}.yaml")
    if not os.path.exists(path):
        print(f"ERROR: Content bank not found: {path}")
        return None, None
    with open(path) as f:
        return yaml.safe_load(f), path


def save_content_bank(cb, path):
    backup = path + ".bak"
    if os.path.exists(path):
        os.rename(path, backup)
    with open(path, "w") as f:
        yaml.dump(cb, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def normalize_products(cb):
    """Normalize products to flat list, handling both {featured: [...]} and flat list formats."""
    products_raw = cb.get("products", [])
    if isinstance(products_raw, dict):
        flat = []
        for key in ("featured", "products", "tours", "top", "others"):
            vals = products_raw.get(key, [])
            if isinstance(vals, list):
                flat.extend(vals)
        return flat if flat else list(products_raw.values())[0] if products_raw else []
    return products_raw if isinstance(products_raw, list) else []


def find_thin_products(cb, target_code=None):
    """Find products with missing or thin data."""
    products = normalize_products(cb)
    thin = []

    for p in products:
        code = p.get("viator_id") or p.get("code", "")
        if target_code and code != target_code:
            continue

        issues = []
        # Check required fields
        for field in REQUIRED_PRODUCT_FIELDS:
            val = p.get(field, "")
            if not val or (isinstance(val, str) and len(val.strip()) < 10):
                issues.append(f"missing_{field}")

        # Check blurb length
        blurb = p.get("custom_blurb", "")
        if blurb and len(blurb) < MIN_BLURB_CHARS:
            issues.append(f"short_blurb ({len(blurb)} chars)")

        # Check recommended fields
        for field in RECOMMENDED_PRODUCT_FIELDS:
            if p.get(field) is None or p.get(field) == "":
                issues.append(f"missing_{field}")

        if issues:
            thin.append({"product": p, "code": code, "issues": issues})

    return thin


def check_knowledge_thin(cb):
    """Check if knowledge section is thin."""
    knowledge = cb.get("knowledge", {})
    issues = []

    facts = knowledge.get("facts", [])
    if len(facts) < MIN_FACTS:
        issues.append(f"facts: {len(facts)} items (min {MIN_FACTS})")

    stories = cb.get("personal_stories", [])
    if len(stories) < MIN_STORIES:
        issues.append(f"personal_stories: {len(stories)} items (min {MIN_STORIES})")

    local_tips = knowledge.get("local_tips", [])
    if not local_tips:
        issues.append("local_tips: empty")

    return issues


def enrich_product(product, code, details, cb):
    """Enrich a product entry with Viator API data."""
    changed = []

    if details:
        # Auto-generate blurb if missing or short
        if "missing_custom_blurb" in product["issues"] or "short_blurb" in product["issues"]:
            if details.get("auto_blurb"):
                product["product"]["custom_blurb"] = details["auto_blurb"]
                changed.append("custom_blurb")

        # Auto-generate best_for if missing
        if "missing_best_for" in product["issues"]:
            if details.get("auto_best_for"):
                product["product"]["best_for"] = details["auto_best_for"]
                changed.append("best_for")

        # Fill rating/reviews if missing
        if "missing_rating" in product["issues"] and details.get("rating"):
            product["product"]["rating"] = details["rating"]
            changed.append("rating")
        if "missing_reviews" in product["issues"] and details.get("reviews"):
            product["product"]["reviews"] = details["reviews"]
            changed.append("reviews")

    return changed


def enrich_knowledge(cb, slug, niche, destination):
    """Enrich the knowledge section from gbrain."""
    knowledge = cb.setdefault("knowledge", {})
    changed = []

    gb_facts = query_gbrain_knowledge(slug, niche, destination)
    if gb_facts and len(knowledge.get("facts", [])) < MIN_FACTS:
        existing = set(knowledge.get("facts", []))
        new_facts = [f for f in gb_facts if f not in existing]
        if new_facts:
            knowledge.setdefault("facts", []).extend(new_facts[:MIN_FACTS])
            changed.append(f"facts: +{len(new_facts[:MIN_FACTS])} from gbrain")

    return changed


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 content_bank_enricher.py <site_slug> [product_code]")
        sys.exit(1)

    slug = sys.argv[1]
    target_code = sys.argv[2] if len(sys.argv) > 2 else None

    cb, path = load_content_bank(slug)
    if not cb:
        sys.exit(1)

    site = cb.get("site", {})
    niche = site.get("niche", "travel")
    destination = site.get("destination", niche)

    print(f"Content Bank Enricher — {slug} — {datetime.now().isoformat()[:19]}")
    print(f"  Niche: {niche} | Destination: {destination}")

    # Phase 1: Detect thin products
    thin_products = find_thin_products(cb, target_code)
    knowledge_issues = check_knowledge_thin(cb)

    if not thin_products and not knowledge_issues:
        print("  ✅ Content bank is well-populated — nothing to enrich.")
        sys.exit(0)

    # Report what's thin
    if thin_products:
        print(f"\n  🔍 Thin products: {len(thin_products)}")
        for tp in thin_products:
            title = tp["product"].get("title", tp["code"])[:60]
            print(f"    {tp['code']}: {', '.join(tp['issues'])} — {title}")

    if knowledge_issues:
        print(f"\n  🔍 Thin knowledge: {', '.join(knowledge_issues)}")

    # Phase 2: Enrich from Viator API
    total_enriched = 0
    for tp in thin_products:
        code = tp["code"]
        print(f"\n  📡 Fetching Viator details for {code}...")
        details = fetch_product_details(code)
        if details:
            changed = enrich_product(tp, code, details, cb)
            if changed:
                print(f"    ✅ Enriched: {', '.join(changed)}")
                total_enriched += len(changed)
            else:
                print(f"    ⚠️  API returned data but no fields needed enrichment")
        else:
            print(f"    ❌ Viator API returned no data for {code}")

        time.sleep(0.3)  # polite spacing

    # Phase 3: Enrich knowledge from gbrain
    if knowledge_issues:
        print(f"\n  🧠 Querying gbrain for {niche} knowledge...")
        k_changes = enrich_knowledge(cb, slug, niche, destination)
        if k_changes:
            print(f"    ✅ Enriched: {', '.join(k_changes)}")
            total_enriched += len(k_changes)
        else:
            print(f"    ⚠️  gbrain returned no new facts")

    # Phase 4: Save
    if total_enriched > 0:
        save_content_bank(cb, path)
        print(f"\n  💾 Saved: {path} ({total_enriched} enrichments)")
    else:
        print(f"\n  ⚠️  No enrichments applied — content bank unchanged")

    sys.exit(0 if total_enriched > 0 else 1)


if __name__ == "__main__":
    main()
