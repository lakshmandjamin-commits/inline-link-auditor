#!/usr/bin/env python3
"""
Lighthouse audit runner. Runs on key pages per site — discovered from
actual directory structure (no hardcoded paths). Homepage + top 5
shallow HTML files by directory depth.

Usage: python3 lighthouse.py [all|site_id]
Requires: Chrome installed at /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
"""
import sqlite3, os, sys, subprocess, json, time
from datetime import datetime
from pathlib import Path

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MAX_PAGES = 6  # homepage + 5 more

def get_sites(target="all"):
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute(
            "SELECT site_id, domain, local_path FROM sites WHERE status='active'"
        ).fetchall()
    else:
        sites = reg.execute(
            "SELECT site_id, domain, local_path FROM sites WHERE site_id=? AND status='active'",
            (target,),
        ).fetchall()
    reg.close()
    return sites


def discover_key_pages(site_path):
    """Find homepage + top shallow HTML files. Excludes /de/, /es/, /backup/."""
    pages = []
    root = Path(site_path)
    if not root.is_dir():
        return ["/"]  # fallback

    for html_file in sorted(root.rglob("*.html")):
        rel = html_file.relative_to(root)
        parts = rel.parts

        # Skip non-English, backups, state dirs
        if any(skip in parts for skip in ("de", "es", "backup", "state", "node_modules")):
            continue

        # Count directory depth (root HTML = 1 part = depth 0)
        depth = len(parts) - 1

        # Build URL path
        url_path = "/" + str(rel).replace("index.html", "")
        if url_path.endswith("//"):
            url_path = "/"
        url_path = url_path.rstrip("/") if url_path != "/" else url_path

        pages.append((url_path, depth))

    # Sort by depth (homepage first, then shallow), take top MAX_PAGES
    pages.sort(key=lambda x: (x[1], x[0]))
    return [p[0] for p in pages[:MAX_PAGES]]

def run_lighthouse(url):
    """Run Lighthouse on URL, return (perf, a11y, seo, best) scores."""
    try:
        r = subprocess.run(
            ["npx", "lighthouse", url, "--output=json", "--quiet",
             "--chrome-flags=--headless --no-sandbox",
             f"--chrome-path={CHROME_PATH}"],
            capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout)
        cats = data.get("categories", {})
        perf = int(cats.get("performance", {}).get("score", 0) * 100)
        a11y = int(cats.get("accessibility", {}).get("score", 0) * 100)
        seo = int(cats.get("seo", {}).get("score", 0) * 100)
        best = int(cats.get("best-practices", {}).get("score", 0) * 100)
        return perf, a11y, seo, best
    except Exception as e:
        print(f"  [WARN] Lighthouse parse failed: {e}", file=sys.stderr)
        return 0, 0, 0, 0

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"Lighthouse Audit — {target} — {datetime.now().isoformat()[:19]}\n")

    sites = get_sites(target)
    for site_id, domain, local_path in sites:
        print(f"--- {site_id} ({domain}) ---")

        # Discover site-specific key pages from directory structure
        key_pages = discover_key_pages(local_path)
        if not key_pages:
            key_pages = ["/"]
        print(f"  Pages: {key_pages}")

        scores = {"perf": [], "a11y": [], "seo": [], "best": []}

        for page in key_pages:
            url = f"https://{domain}{page}"
            perf, a11y, seo, best = run_lighthouse(url)
            scores["perf"].append(perf)
            scores["a11y"].append(a11y)
            scores["seo"].append(seo)
            scores["best"].append(best)
            print(f"  {page}: P{perf} A{a11y} S{seo} B{best}")
            time.sleep(5)  # Don't hammer Lighthouse

        avg_perf = sum(scores["perf"]) // len(scores["perf"])
        avg_a11y = sum(scores["a11y"]) // len(scores["a11y"])
        avg_seo = sum(scores["seo"]) // len(scores["seo"])
        avg_best = sum(scores["best"]) // len(scores["best"])

        print(f"  Averages: P{avg_perf} A{avg_a11y} S{avg_seo} B{avg_best}")

        # Update DB
        reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
        reg.execute("""UPDATE sites SET lighthouse_perf=?, lighthouse_a11y=?,
            lighthouse_seo=?, lighthouse_best=?, last_validated_at=CURRENT_TIMESTAMP WHERE site_id=?""",
            (avg_perf, avg_a11y, avg_seo, avg_best, site_id))
        reg.execute("""INSERT INTO audit_log (site_id, check_type, status, issues_found, details)
            VALUES (?, 'lighthouse', ?, ?, ?)""",
            (site_id, 'pass' if avg_perf >= 80 else 'warn', 0,
             f"P{avg_perf}/A{avg_a11y}/S{avg_seo}/B{avg_best}"))
        reg.commit()
        reg.close()

    print(f"\n{'='*50}")
    print("Lighthouse audit complete.")

if __name__ == "__main__":
    main()
