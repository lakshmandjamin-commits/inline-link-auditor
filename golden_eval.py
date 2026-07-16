#!/usr/bin/env python3
"""
Golden page eval — detects model/pipeline drift by auditing reference pages.

Each of the 4 sites has one golden page. The eval measures 8 quality dimensions
and compares against known-good baselines. Alerts if any dimension degrades.

Usage: python3 golden_eval.py [--baseline] [--check]
  --baseline: record current metrics as the baseline (run once per model change)
  --check:    compare current metrics against baseline (run weekly via cron)

Dimensions:
  1. Word count (editorial body)
  2. Anti-words (0 expected)
  3. Product cards (count)
  4. Local wisdom markers (≥1)
  5. Negative recommendations (≥2)
  6. Inline Viator link in first 400 words
  7. JSON-LD Product schema
  8. Heading hierarchy (H1→H2→H3 order)
"""

import os, sys, re, json, sqlite3
from pathlib import Path
from collections import defaultdict
from datetime import datetime

# --- Configuration ---

GOLDEN_PAGES = {
    "tenerife-outdoor-guide": {
        "file": "tenerife-hiking-beginners-guide.html",
        "type": "comparison",
        "description": "Teide beginners guide — narrative + product integration"
    },
    "madeira-trail-guide": {
        "file": "pr1-vs-pr1-2-pico-ruivo-comparison.html",
        "type": "comparison",
        "description": "PR1 vs PR1.2 comparison — detailed editorial comparison"
    },
    "porto-sommelier": {
        "file": "porto-food-tours.html",
        "type": "editorial",
        "description": "Porto food tours — local wisdom + product integration"
    },
    "lapland-adventure-guide": {
        "file": "winter-holiday-planner.html",
        "type": "hub",
        "description": "Winter holiday planner — hub page with practical tips"
    }
}

BASELINE_FILE = os.path.expanduser("~/.hermes/affiliate-crons/state/golden_eval_baseline.json")

# Local wisdom markers (from Rule 59)
LOCAL_WISDOM_RE = re.compile(
    r'(I learned|I discovered|nobody tells you|they don.t tell you|'
    r'the hard way|This is not normal|surprised me|I was not prepared)',
    re.IGNORECASE
)

# Negative recommendation markers (from Rule 31)
NEGATIVE_REC_RE = re.compile(
    r'(not for you if|not recommended|not worth|skip this|avoid|'
    r'don.t bother|not suitable|pass on|give this a miss)',
    re.IGNORECASE
)

# Viator link in first 400 words of body (Rule 32)
VIATOR_LINK_RE = re.compile(r'viator\.com.*pid=P00303273', re.IGNORECASE)

# Product JSON-LD schema
PRODUCT_SCHEMA_RE = re.compile(r'"@type"\s*:\s*"Product"')
FAQ_SCHEMA_RE = re.compile(r'"@type"\s*:\s*"FAQPage"')  # DEPRECATED: FAQPage dead for SERP 2026 — presence is a WARNING


def _walk_for_file(base_dir, filename):
    """Walk a directory up to 3 levels deep looking for a file. Returns list of paths."""
    results = []
    if not os.path.isdir(base_dir):
        return results
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if 'backup' not in d.lower()
                   and d != '.git' and d != 'node_modules']
        depth = root[len(base_dir):].count(os.sep)
        if depth > 3:
            dirs.clear()
            continue
        if filename in files:
            results.append(os.path.join(root, filename))
    return results


def find_html_file(site_slug, filename):
    """Locate the golden page HTML file on disk."""
    import glob
    candidates = []

    # Direct paths — also check registered site paths
    for base in [
        os.path.expanduser(f"~/sites/{site_slug}"),
        os.path.expanduser(f"~/sites/{site_slug}/sites/{site_slug}"),
    ]:
        candidates.extend(_walk_for_file(base, filename))

    # Also check any ~/sites/*/sites/{site_slug} pattern (nested Madeira-style)
    import glob as gl
    for nested_base in gl.glob(os.path.expanduser(f"~/sites/*/sites/{site_slug}")):
        candidates.extend(_walk_for_file(nested_base, filename))

    # Deduplicate, prefer non-backup paths
    candidates = [p for p in candidates if 'backup' not in p.lower()]
    return candidates[0] if candidates else None


def extract_body_text(html):
    """Extract <main> body text, excluding <header>, <nav>, <footer>, <script>."""
    # Get content between <main> and </main>
    main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
    if not main_match:
        return ""
    body = main_match.group(1)
    # Strip HTML tags for word count
    body = re.sub(r'<script[^>]*>.*?</script>', '', body, flags=re.DOTALL)
    body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL)
    body = re.sub(r'<[^>]+>', ' ', body)
    body = re.sub(r'\s+', ' ', body).strip()
    return body


def measure_page(html_path):
    """Measure all 8 quality dimensions for a page."""
    path = Path(html_path)
    html = path.read_text(errors="ignore")
    body_text = extract_body_text(html)

    word_count = len(body_text.split())
    product_cards = len(re.findall(r'class="[^"]*product-card[^"]*"', html))
    local_wisdom = len(LOCAL_WISDOM_RE.findall(body_text))
    negative_recs = len(NEGATIVE_REC_RE.findall(body_text))

    # Inline link in first 400 words — check raw HTML for Viator links in <main>
    main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
    main_content = main_match.group(1) if main_match else ""
    # Take first 400 words and check for <a href="...viator.com...pid=P00303273..."
    main_words = re.split(r'\s+', re.sub(r'<[^>]+>', ' ', main_content).strip())
    first_400_raw = ' '.join(main_words[:400])
    inline_early = bool(re.search(
        r'href="[^"]*viator\.com[^"]*pid=P00303273',
        first_400_raw, re.IGNORECASE
    )) or bool(re.search(
        r'href="[^"]*viator\.com[^"]*pid=P00303273',
        main_content[:2000], re.IGNORECASE  # Fallback: check first 2000 chars of HTML
    ))

    # JSON-LD
    has_product_schema = bool(PRODUCT_SCHEMA_RE.search(html))
    has_faq_schema = bool(FAQ_SCHEMA_RE.search(html))

    # Heading hierarchy
    h1_count = len(re.findall(r'<h1[ >]', html))
    h2_count = len(re.findall(r'<h2[ >]', html))
    h3_count = len(re.findall(r'<h3[ >]', html))
    h_tags_valid = h1_count == 1 and h2_count > 0

    # Anti-words (simple grep — antiword_scan.py handles Unicode properly)
    anti_words_en = len(re.findall(
        r'\b(immersive|unforgettable|breathtaking|world.class|seamless|curated|'
        r'life.changing|unparalleled|hidden.gem)\b',
        body_text, re.IGNORECASE
    ))

    return {
        "word_count": word_count,
        "anti_words": anti_words_en,
        "product_cards": product_cards,
        "local_wisdom": local_wisdom,
        "negative_recs": negative_recs,
        "inline_link_first_400": inline_early,
        "has_product_schema": has_product_schema,
        "has_faq_schema": has_faq_schema,
        "heading_valid": h_tags_valid,
        "h1_count": h1_count,
        "h2_count": h2_count,
        "h3_count": h3_count,
    }


def check_against_baseline(metrics, baseline, page_name):
    """Compare current metrics against baseline. Returns list of issues.
    
    Uses baseline values with 20% tolerance for word count, 0 tolerance
    for anti-words/schema/headings (must never regress from 0 or True).
    Local wisdom and negative recs use the baseline floor — can't go below
    what was recorded.
    """
    issues = []
    
    # Word count: allow 20% degradation from baseline
    wc_baseline = baseline.get("word_count", 0)
    if metrics["word_count"] < wc_baseline * 0.8:
        issues.append(f"  ❌ word_count: {metrics['word_count']}w (baseline: {wc_baseline}w, floor: {int(wc_baseline*0.8)}w)")
    
    # Anti-words: must be 0 if baseline was 0
    if baseline.get("anti_words", 0) == 0 and metrics["anti_words"] > 0:
        issues.append(f"  ❌ anti_words: {metrics['anti_words']} found (baseline: 0)")
    
    # Product cards: can't drop below baseline
    pc_baseline = baseline.get("product_cards", 0)
    if metrics["product_cards"] < pc_baseline:
        issues.append(f"  ❌ product_cards: {metrics['product_cards']} (baseline: {pc_baseline})")
    
    # Local wisdom: can't drop below baseline floor
    lw_baseline = baseline.get("local_wisdom", 0)
    if metrics["local_wisdom"] < lw_baseline:
        issues.append(f"  ❌ local_wisdom: {metrics['local_wisdom']} markers (baseline: {lw_baseline})")
    
    # Negative recs: can't drop below baseline floor
    nr_baseline = baseline.get("negative_recs", 0)
    if metrics["negative_recs"] < nr_baseline:
        issues.append(f"  ❌ negative_recs: {metrics['negative_recs']} (baseline: {nr_baseline})")
    
    # Inline link: if baseline had it, can't lose it
    if baseline.get("inline_link_first_400") and not metrics["inline_link_first_400"]:
        issues.append(f"  ❌ inline_link_first_400: lost (baseline had it)")
    
    # Schema: if baseline had it, can't lose it
    if baseline.get("has_product_schema") and not metrics["has_product_schema"]:
        issues.append(f"  ❌ has_product_schema: lost (baseline had it)")
    # FAQPage is deprecated (dead for SERP 2026) — presence is a WARNING, not a bonus
    if baseline.get("has_faq_schema"):
        issues.append(f"  ⚠️ DEPRECATED: baseline has FAQPage schema (dead for Google SERP 2026 — should be Article)")
    if metrics.get("has_faq_schema") and not baseline.get("has_faq_schema"):
        issues.append(f"  ⚠️ DEPRECATED: new page has FAQPage schema (should use Article instead)")
    
    # Headings: if baseline was valid, can't regress
    if baseline.get("heading_valid") and not metrics["heading_valid"]:
        issues.append(f"  ❌ heading_valid: H1={metrics['h1_count']} H2={metrics['h2_count']} (baseline: valid)")
    
    return issues


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"

    if mode not in ("--baseline", "--check"):
        print(f"Usage: python3 golden_eval.py [--baseline|--check]")
        sys.exit(1)

    all_metrics = {}
    total_issues = []

    print(f"Golden Page Eval — {mode} — {datetime.now().isoformat()[:19]}\n")

    for site_slug, config in GOLDEN_PAGES.items():
        filepath = find_html_file(site_slug, config["file"])
        if not filepath:
            print(f"❌ {site_slug}: golden page not found ({config['file']})")
            continue

        metrics = measure_page(filepath)
        all_metrics[site_slug] = {
            "file": config["file"],
            "type": config["type"],
            "path": filepath,
            "metrics": metrics,
            "description": config["description"]
        }

        print(f"--- {site_slug}: {config['description']} ---")
        print(f"    File: {filepath}")
        print(f"    Words: {metrics['word_count']} | Anti-words: {metrics['anti_words']} | "
              f"Product cards: {metrics['product_cards']} | Local wisdom: {metrics['local_wisdom']} | "
              f"Negative recs: {metrics['negative_recs']} | Inline link: {metrics['inline_link_first_400']} | "
              f"Schema: P={metrics['has_product_schema']} F={metrics['has_faq_schema']} | "
              f"Headings: H1={metrics['h1_count']} H2={metrics['h2_count']} H3={metrics['h3_count']}")

        if mode == "--check":
            # Load baseline
            if os.path.exists(BASELINE_FILE):
                with open(BASELINE_FILE) as f:
                    baseline = json.load(f)
                site_baseline = baseline.get(site_slug, {}).get("metrics", {})
                if site_baseline:
                    issues = check_against_baseline(metrics, site_baseline, site_slug)
                    if issues:
                        total_issues.extend(issues)
                        for issue in issues:
                            print(issue)
                    else:
                        print(f"    ✅ All dimensions pass")
                else:
                    print(f"    ⚠️  No baseline for {site_slug}")
            else:
                print(f"    ⚠️  No baseline file — run --baseline first")

        print()

    # Save baseline if requested
    if mode == "--baseline":
        output = {}
        for slug, data in all_metrics.items():
            output[slug] = {
                "file": data["file"],
                "type": data["type"],
                "description": data["description"],
                "path": data["path"],
                "metrics": data["metrics"],
                "recorded_at": datetime.now().isoformat()
            }
        os.makedirs(os.path.dirname(BASELINE_FILE), exist_ok=True)
        with open(BASELINE_FILE, 'w') as f:
            json.dump(output, f, indent=2)
        print(f"✅ Baseline saved: {BASELINE_FILE}")
        print(f"   Run '--check' to compare future runs against this baseline.")

    # Summary
    if mode == "--check":
        total_dims = len(GOLDEN_PAGES) * 8
        passed = total_dims - len(total_issues)
        print(f"{'='*50}")
        print(f"Eval complete: {passed}/{total_dims} dimensions pass")
        if total_issues:
            print(f"\nIssues found:")
            for issue in total_issues:
                print(issue)
            sys.exit(1)
        else:
            print("✅ All golden pages within baseline thresholds")


if __name__ == "__main__":
    main()
