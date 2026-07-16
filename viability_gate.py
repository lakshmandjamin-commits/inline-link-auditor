#!/usr/bin/env python3
"""
PRE-Phase-0 Destination Viability Gate — runs BEFORE any content bank work.
Validates that a Viator destination has enough bookable products to justify a site build.

Two modes:
  1. DB-only (default): Reads from viator_cli.db. Works for known destinations.
  2. API fetch (--fetch): Calls Viator Partner API for the destination, stores results
     temporarily, then analyzes. Required for NEW destinations not yet in the DB.

Usage:
  python3 viability_gate.py <site_slug> --destination-id <N> --plan <plan.yaml> [--fetch] [--verbose]

Output:
  Writes /tmp/viability-{site_slug}.json with go/no-go, tier, topic allowlist, warnings.
  Exit codes: 0=go, 2=no-go, 1=error
"""
import sys, os, json, sqlite3, argparse, re, urllib.request, tempfile, time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, timedelta

DB_PATH = Path.home() / ".hermes" / "affiliate-crons" / "db" / "viator_cli.db"
ENV_PATH = Path.home() / ".hermes" / ".env"
API_BASE = "https://api.viator.com/partner"
API_KEY = None

def _load_api_key():
    global API_KEY
    if API_KEY:
        return API_KEY
    if ENV_PATH.exists():
        for line in open(ENV_PATH).readlines():
            if line.startswith("VIATOR_API_KEY="):
                API_KEY = line.split("=", 1)[1].strip()
                return API_KEY
    # Fallback: try environment
    API_KEY = os.environ.get("VIATOR_API_KEY")
    return API_KEY

# ── Thresholds (calibration candidates) ──────────────────────────────────────
THRESHOLDS = {
    "min_total_products": 15,       # Lowered from 25 since DB+API pooling
    "min_products_money_page": 3,
    "min_review_count": 5,          # Lowered from 10
    "min_proven_share": 0.30,       # Lowered from 0.40
    "min_money_topics": 3,
    "transactional_target": 0.70,
    "split_tolerance": 0.15,        # Relaxed from 0.10
    "min_price_coverage": 0.50,     # Lowered from 0.60
    "max_stale_days": 365,          # Relaxed from 180
    "min_review_score": 3.5,
    # Score weights
    "w_total_products": 0.30,
    "w_proven_share": 0.25,
    "w_topic_pass_rate": 0.25,
    "w_price_coverage": 0.10,
    "w_split_fit": 0.10,
    # Tier cutoffs
    "tier_a": 75,
    "tier_b": 55,
    "tier_c": 40,
}

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class TopicSpec:
    topic: str
    is_money_page: bool = True
    match_terms: list[str] = field(default_factory=list)

@dataclass
class ProductRow:
    product_code: str
    title: str
    review_count: int = 0
    rating: float = 0.0
    active: int = 1
    is_available: int = 1
    last_synced: str = ""
    last_available: str = ""
    has_price: bool = False
    source: str = "db"  # "db" or "api"

@dataclass
class TopicResult:
    topic: str
    is_money_page: bool
    n_products: int = 0
    n_proven: int = 0
    n_stale: int = 0
    n_degraded: int = 0
    has_quality_product: bool = False
    primary_product_code: Optional[str] = None
    intent_type: str = "transactional"
    passed: bool = False
    reason: Optional[str] = None

@dataclass
class ViabilityReport:
    site: str
    destination_id: int
    generated_at: str
    source_mode: str = "api"   # "api" or "db-advisory"
    go: bool = False
    tier: str = "NO-GO"
    viability_score: float = 0.0
    totals: dict = field(default_factory=dict)
    split: dict = field(default_factory=dict)
    recommended_money_page_count: int = 0
    topic_allowlist: list[dict] = field(default_factory=list)
    topic_blocklist: list[dict] = field(default_factory=list)
    quality_warnings: list[dict] = field(default_factory=list)
    failed_gates: list[str] = field(default_factory=list)

# ── Viator API fetch ──────────────────────────────────────────────────────────

def api_headers():
    key = _load_api_key()
    if not key:
        raise RuntimeError("VIATOR_API_KEY not found in ~/.hermes/.env")
    return {
        "Accept": "application/json;version=2.0",
        "Accept-Language": "en",
        "exp-api-key": key,
    }

def fetch_products_from_api(dest_id: int) -> list[ProductRow]:
    """Fetch all products for a destination from Viator Partner API.
    
    Uses POST /partner/products/search with filtering.destination.
    Paginates to get all products.
    """
    headers = api_headers()
    headers["Content-Type"] = "application/json"
    all_products = []
    start = 0
    count = 100
    total = None

    while total is None or start < total:
        body = json.dumps({
            "filtering": {"destination": dest_id},
            "currency": "USD",
            "pagination": {"start": start, "count": count}
        }).encode()

        req = urllib.request.Request(
            f"{API_BASE}/products/search",
            data=body,
            headers=headers,
            method="POST"
        )
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()[:200]
            raise RuntimeError(
                f"Viator API error {e.code}: {body_text}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Viator API fetch failed: {e}") from e

        products = data.get("products", [])
        if total is None:
            total = data.get("totalCount", len(products))

        for p in products:
            all_products.append(ProductRow(
                product_code=p.get("productCode", ""),
                title=p.get("title", ""),
                review_count=p.get("reviews", {}).get("totalReviews", 0) if p.get("reviews") else 0,
                rating=p.get("reviews", {}).get("combinedAverageRating", 0) if p.get("reviews") else 0,
                active=1,
                is_available=1,
                last_synced=datetime.now().isoformat()[:10],
                last_available=datetime.now().isoformat()[:10],
                has_price=bool(p.get("pricing")),
                source="api",
            ))

        start += count
        time.sleep(0.5)  # Rate limit courtesy

    return all_products

# ── DB queries ────────────────────────────────────────────────────────────────

def _connect(db_path=None):
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def load_db_products(conn) -> list[ProductRow]:
    """Load all active products from the local DB."""
    rows = conn.execute("""
        SELECT product_code, title, review_count, rating, active, is_available,
               last_synced, last_available
        FROM products
        WHERE active = 1
    """).fetchall()
    return [ProductRow(
        product_code=r["product_code"], title=r["title"],
        review_count=r["review_count"] or 0, rating=r["rating"] or 0,
        active=r["active"] or 1, is_available=r["is_available"] or 1,
        last_synced=r["last_synced"] or "", last_available=r["last_available"] or "",
        has_price=False, source="db",
    ) for r in rows]

def price_coverage_from_db(conn) -> float:
    row = conn.execute("""
        SELECT COUNT(DISTINCT p.product_code) AS total,
               COUNT(DISTINCT pr.product_code) AS with_price
        FROM products p
        LEFT JOIN prices pr ON pr.product_code = p.product_code
        WHERE p.active = 1
    """).fetchone()
    if not row or row["total"] == 0:
        return 0.0
    return row["with_price"] / row["total"]

# ── Analysis functions ────────────────────────────────────────────────────────

def stale_cutoff_date() -> str:
    return (datetime.now() - timedelta(days=THRESHOLDS["max_stale_days"])).isoformat()[:10]

def match_topic_products(products: list[ProductRow], terms: list[str]) -> list[ProductRow]:
    """Fuzzy-match products against topic terms."""
    if not terms:
        return []
    results = []
    for p in products:
        title_lower = p.title.lower()
        for term in terms:
            if term.lower() in title_lower:
                results.append(p)
                break
    return results

def evaluate_topic(products: list[ProductRow], spec: TopicSpec) -> TopicResult:
    result = TopicResult(
        topic=spec.topic,
        is_money_page=spec.is_money_page,
        intent_type="transactional" if spec.is_money_page else "informational",
    )
    matches = match_topic_products(products, spec.match_terms)
    if not matches:
        result.reason = "no_matching_products"
        return result

    result.n_products = len(matches)
    stale_date = stale_cutoff_date()

    for p in matches:
        is_active = p.active == 1 and p.is_available == 1
        has_reviews = p.review_count > 0
        is_stale = p.last_available and p.last_available < stale_date

        if not is_active:
            result.n_degraded += 1
        elif is_stale:
            result.n_stale += 1
        elif has_reviews:
            result.n_proven += 1

        if p.review_count >= THRESHOLDS["min_review_count"] and p.rating >= THRESHOLDS["min_review_score"]:
            result.has_quality_product = True
            if result.primary_product_code is None:
                result.primary_product_code = p.product_code

    if result.primary_product_code is None and matches:
        result.primary_product_code = matches[0].product_code

    if spec.is_money_page:
        fails = []
        if result.n_products < THRESHOLDS["min_products_money_page"]:
            fails.append(f"below_min_products ({result.n_products} < {THRESHOLDS['min_products_money_page']})")
        if not result.has_quality_product:
            fails.append("no_quality_product")
        if fails:
            result.reason = "; ".join(fails)
            return result
        result.passed = True
    else:
        result.passed = result.n_products >= 1
        if not result.passed:
            result.reason = "no_products_for_informational"

    return result

def build_warnings(products: list[ProductRow]) -> list[dict]:
    warnings = []
    stale_date = stale_cutoff_date()
    for p in products:
        issues = []
        if p.active != 1 or p.is_available != 1:
            issues.append("degraded")
        if p.last_available and p.last_available < stale_date:
            issues.append(f"stale_since_{p.last_available}")
        if p.review_count == 0:
            issues.append("zero_reviews")
        if issues:
            warnings.append({"product_code": p.product_code, "title": p.title, "issues": issues})
    return warnings[:30]

def compute_destination_quality(products: list[ProductRow]) -> dict:
    total = len(products)
    proven = sum(1 for p in products if p.active == 1 and p.is_available == 1 and p.review_count > 0)
    degraded = sum(1 for p in products if p.active != 1 or p.is_available != 1)
    zero_reviews = sum(1 for p in products if p.review_count == 0)
    reviews = [p.review_count for p in products if p.review_count and p.review_count > 0]
    ratings = [p.rating for p in products if p.rating and p.rating > 0]
    return {
        "total": total,
        "proven": proven,
        "degraded": degraded,
        "proven_share": round(proven / total, 3) if total else 0,
        "zero_reviews": zero_reviews,
        "avg_review_count": round(sum(reviews) / len(reviews), 1) if reviews else 0,
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
    }

# ── Main gate logic ───────────────────────────────────────────────────────────

def run_gate(site: str, dest_id: int, planned_topics: list[TopicSpec],
             fetch: bool = False, verbose: bool = False) -> ViabilityReport:
    report = ViabilityReport(
        site=site, destination_id=dest_id,
        generated_at=datetime.now().isoformat(),
    )

    # ── Load products ─────────────────────────────────────────────────────────
    products: list[ProductRow] = []

    if fetch:
        if verbose:
            print(f"Fetching products from Viator API for destId={dest_id}...")
        api_products = fetch_products_from_api(dest_id)
        if verbose:
            print(f"  API returned {len(api_products)} products")
        products.extend(api_products)

    # Also load from DB for pricing info
    if DB_PATH.exists():
        conn = _connect()
        db_products = load_db_products(conn)
        if verbose and db_products:
            print(f"  DB has {len(db_products)} products (topic keyword matching)")
        # Merge: API products take priority, DB fills pricing gaps
        api_codes = {p.product_code for p in products}
        for p in db_products:
            if p.product_code not in api_codes:
                products.append(p)
        price_cov = price_coverage_from_db(conn)
        conn.close()
    else:
        price_cov = 0.0
        # Estimate from API products
        with_price = sum(1 for p in products if p.has_price)
        if products:
            price_cov = with_price / len(products)

    if not products:
        report.go = False
        report.failed_gates = ["no_products_at_all"]
        report.tier = "NO-GO"
        return report

    # ── Quality aggregates ────────────────────────────────────────────────────
    quality = compute_destination_quality(products)
    report.totals = {
        "total_products": quality["total"],
        "proven": quality["proven"],
        "degraded": quality["degraded"],
        "proven_share": quality["proven_share"],
        "avg_review_count": quality["avg_review_count"],
        "avg_rating": quality["avg_rating"],
        "price_coverage": round(price_cov, 3),
    }

    # ── Per-topic evaluation ──────────────────────────────────────────────────
    topic_results = []
    for spec in planned_topics:
        tr = evaluate_topic(products, spec)
        topic_results.append(tr)
        if verbose:
            status = "PASS" if tr.passed else f"FAIL ({tr.reason})"
            print(f"  {spec.topic:30s} n={tr.n_products:3d} proven={tr.n_proven:2d} "
                  f"stale={tr.n_stale:2d} degraded={tr.n_degraded:2d} → {status}")

    money_results = [t for t in topic_results if t.is_money_page]
    info_results = [t for t in topic_results if not t.is_money_page]
    passing_money = [t for t in money_results if t.passed]

    # ── Split analysis ────────────────────────────────────────────────────────
    total_info_planned = len(info_results)
    achievable_share = len(passing_money) / (len(passing_money) + total_info_planned) \
        if (len(passing_money) + total_info_planned) > 0 else 0
    split_achievable = abs(achievable_share - THRESHOLDS["transactional_target"]) \
        <= THRESHOLDS["split_tolerance"]

    report.split = {
        "money_topics_passing": len(passing_money),
        "money_topics_planned": len(money_results),
        "informational_topics_planned": total_info_planned,
        "achievable_transactional_share": round(achievable_share, 3),
        "target": THRESHOLDS["transactional_target"],
        "achievable": split_achievable,
    }

    # ── Allowlist / blocklist ─────────────────────────────────────────────────
    for tr in passing_money + [t for t in info_results if t.passed]:
        report.topic_allowlist.append({
            "topic": tr.topic, "n_products": tr.n_products,
            "n_proven": tr.n_proven, "n_stale": tr.n_stale,
            "n_degraded": tr.n_degraded,
            "primary_product_code": tr.primary_product_code,
            "intent_type": tr.intent_type,
        })

    for tr in [t for t in topic_results if not t.passed]:
        report.topic_blocklist.append({
            "topic": tr.topic, "n_products": tr.n_products,
            "reason": tr.reason or "unknown",
            "intent_type": tr.intent_type,
        })

    # ── Destination-level gates ───────────────────────────────────────────────
    total = quality["total"]
    proven_share = quality["proven_share"]
    if total < THRESHOLDS["min_total_products"]:
        report.failed_gates.append(f"total_products ({total} < {THRESHOLDS['min_total_products']})")
    if proven_share < THRESHOLDS["min_proven_share"]:
        report.failed_gates.append(f"proven_share ({proven_share:.2f} < {THRESHOLDS['min_proven_share']})")
    if len(passing_money) < THRESHOLDS["min_money_topics"]:
        report.failed_gates.append(f"min_money_topics ({len(passing_money)} < {THRESHOLDS['min_money_topics']})")
    if price_cov < THRESHOLDS["min_price_coverage"]:
        report.failed_gates.append(f"price_coverage ({price_cov:.2f} < {THRESHOLDS['min_price_coverage']})")
    # Split: if too informational (< target - tolerance), HARD BLOCK (can't earn).
    # If too transactional (> target + tolerance), warn but don't block (more $ is fine).
    split_shortfall = THRESHOLDS["transactional_target"] - THRESHOLDS["split_tolerance"]
    if achievable_share < split_shortfall:
        report.failed_gates.append(
            f"split_too_informational ({achievable_share:.2f} < {split_shortfall:.2f})"
        )
    elif achievable_share > THRESHOLDS["transactional_target"] + THRESHOLDS["split_tolerance"]:
        # Too transactional — not a failure, just note it
        pass

    report.go = len(report.failed_gates) == 0

    # ── Viability score ───────────────────────────────────────────────────────
    w = THRESHOLDS
    norm_total = min(total / w["min_total_products"], 1.0)
    norm_topic = len(passing_money) / len(money_results) if money_results else 0
    split_fit = 1.0 - abs(achievable_share - w["transactional_target"])

    score = 100 * (
        w["w_total_products"] * norm_total +
        w["w_proven_share"] * min(proven_share / w["min_proven_share"], 1.0) +
        w["w_topic_pass_rate"] * norm_topic +
        w["w_price_coverage"] * min(price_cov / w["min_price_coverage"], 1.0) +
        w["w_split_fit"] * split_fit
    )
    report.viability_score = round(score, 1)

    # ── Source mode validation (after gates computed) ──────────────────────────
    report.source_mode = "api" if fetch else "db-advisory"
    if report.source_mode == "db-advisory":
        report.failed_gates.append(
            "DB_ONLY_NOT_DESTINATION_SCOPED: products keyword-matched across "
            "all destinations. Use --fetch for authoritative viability."
        )

    if report.go:
        if score >= w["tier_a"]:
            report.tier = "A"
        elif score >= w["tier_b"]:
            report.tier = "B"
        else:
            report.tier = "C"
    else:
        report.tier = "NO-GO"

    report.recommended_money_page_count = len(passing_money)
    report.quality_warnings = build_warnings(products)

    return report

# ── Plan file parsing ─────────────────────────────────────────────────────────

def parse_plan(plan_path: str) -> list[TopicSpec]:
    path = Path(plan_path)
    topics = []

    if path.suffix in ('.yaml', '.yml'):
        try:
            import yaml
            with open(path) as f:
                data = yaml.safe_load(f)
        except ImportError:
            print("ERROR: PyYAML not installed. pip install pyyaml", file=sys.stderr)
            sys.exit(1)
        for t in data.get("topics", []):
            slug = t.get("slug", "")
            terms = t.get("terms", [slug.replace("-", " ")])
            topics.append(TopicSpec(topic=slug, is_money_page=t.get("money_page", True), match_terms=terms))
    else:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                is_money = not line.startswith("~")
                slug = line.lstrip("~").strip()
                terms = [slug.replace("-", " ")]
                topics.append(TopicSpec(topic=slug, is_money_page=is_money, match_terms=terms))
    return topics

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PRE-Phase-0 Destination Viability Gate")
    parser.add_argument("site", help="Site slug (e.g. yogyakarta-temple-tours)")
    parser.add_argument("--destination-id", type=int, required=True, help="Viator destination ID")
    parser.add_argument("--plan", required=True, help="Path to site plan file")
    parser.add_argument("--db", default=str(DB_PATH), help=f"Path to viator_cli.db (default: {DB_PATH})")
    parser.add_argument("--fetch", action="store_true", help="Fetch products from Viator API first")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.fetch:
        key = _load_api_key()
        if not key:
            print("ERROR: --fetch requires VIATOR_API_KEY in ~/.hermes/.env", file=sys.stderr)
            sys.exit(1)

    planned_topics = parse_plan(args.plan)
    if not planned_topics:
        print(f"ERROR: No topics found in {args.plan}", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(f"Site: {args.site}  Destination ID: {args.destination_id}")
        print(f"Topics: {len(planned_topics)} "
              f"({sum(1 for t in planned_topics if t.is_money_page)} money, "
              f"{sum(1 for t in planned_topics if not t.is_money_page)} info)")
        if args.fetch:
            print("Mode: API fetch + DB")
        print()

    try:
        report = run_gate(args.site, args.destination_id, planned_topics, args.fetch, args.verbose)
    except RuntimeError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)

    out_path = Path(f"/tmp/viability-{args.site}.json")
    out_path.write_text(json.dumps(asdict(report), indent=2, default=str))

    print(f"\n{'✅ GO' if report.go else '❌ NO-GO'}  "
          f"Source: {report.source_mode}  Tier: {report.tier}  "
          f"Score: {report.viability_score}  Money pages: {report.recommended_money_page_count}")
    if report.failed_gates:
        print(f"Failed gates: {', '.join(report.failed_gates)}")
    print(f"Output: {out_path}")

    return 0 if report.go else 2

if __name__ == "__main__":
    sys.exit(main())
