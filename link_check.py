#!/usr/bin/env python3
"""
Broken link scanner. Checks all internal + external links on a site.
Usage: python3 link_check.py [all|site_id]
No API dependencies. Runs locally. Outputs broken links by page.
"""
import sqlite3, os, sys, re, time, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
LOG_DIR = os.path.expanduser("~/.hermes/affiliate-crons/logs")
os.makedirs(LOG_DIR, exist_ok=True)

CHECK_EXTERNAL = "--check-external" in sys.argv

def get_sites(target="all"):
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute("SELECT site_id, local_path, domain FROM sites WHERE status='active'").fetchall()
    else:
        sites = reg.execute("SELECT site_id, local_path, domain FROM sites WHERE site_id=? AND status='active'", (target,)).fetchall()
    reg.close()
    return sites

def get_pages(local_path):
    pages = []
    for f in Path(local_path).rglob("*.html"):
        if "node_modules" not in str(f):
            pages.append(f)
    return pages

def extract_links(html, page_path):
    internal, external = [], []
    for m in re.finditer(r'href="([^"]+)"', html):
        href = m.group(1)
        if href.startswith(("http://", "https://")):
            external.append(href)
        elif not href.startswith(("#", "mailto:", "tel:", "javascript:")):
            internal.append(href)
    return internal, external

def check_internal_link(href, page_dir, local_path, all_pages):
    if href.startswith("/"):
        target = Path(local_path) / href.lstrip("/")
    else:
        target = (page_dir / href).resolve()

    candidates = [target, target.with_suffix(".html"),
                  target / "index.html", Path(str(target) + ".html")]
    return any(c.exists() for c in candidates)

def check_external_link(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Hermes-LinkChecker/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status == 200
    except Exception as e:
        print(f"  [WARN] External link check failed: {e}", file=sys.stderr)
        return False

def check_favicon(domain):
    """Check favicon.svg returns 200."""
    try:
        req = urllib.request.Request(f"https://{domain}/favicon.svg",
            headers={"User-Agent": "Hermes-LinkChecker/1.0"})
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status == 200
    except Exception as e:
        print(f"  [WARN] Favicon check failed: {e}", file=sys.stderr)
        return False

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"Link Check — {target} — {datetime.now().isoformat()[:19]}\n")

    sites = get_sites(target)
    total = {"pages": 0, "links": 0, "broken_internal": 0, "broken_external": 0,
             "skipped_viator": 0, "skipped_external": 0, "issues": []}

    for site_id, local_path, domain in sites:
        print(f"--- {site_id} ---")
        pages = get_pages(local_path)
        all_paths = set(p for p in Path(local_path).rglob("*") if p.is_file())
        site_issues = 0

        for page in pages:
            html = page.read_text(errors="ignore")
            internal, external = extract_links(html, page)
            total["links"] += len(internal) + len(external)

            for link in internal:
                if not check_internal_link(link, page.parent, local_path, all_paths):
                    total["broken_internal"] += 1
                    issue = f"{page.relative_to(local_path)} -> {link}"
                    total["issues"].append(issue)
                    site_issues += 1
                    print(f"  BROKEN: {issue}")

            for link in external:
                if 'viator.com' in link:
                    total["skipped_viator"] += 1
                    continue
                if CHECK_EXTERNAL:
                    if not check_external_link(link):
                        total["broken_external"] += 1
                        issue = f"{page.relative_to(local_path)} -> {link[:80]}"
                        total["issues"].append(issue)
                        site_issues += 1
                        print(f"  DEAD: {issue}")
                else:
                    total["skipped_external"] += 1

            # NEW: Check for broken Viator product codes (d5392-? pattern)
            broken_codes = re.findall(r'viator\.com/[^"]*?/d\d+-\?', html)
            for bc in broken_codes:
                site_issues += 1
                issue = f"{page.relative_to(local_path)} -> BROKEN VIATOR CODE: {bc[:80]}"
                total["issues"].append(issue)
                print(f"  BROKEN VIATOR: {issue}")

        total["pages"] += len(pages)
        
        # NEW: Check favicon
        if not check_favicon(domain):
            site_issues += 1
            issue = f"FAVICON MISSING: https://{domain}/favicon.svg"
            total["issues"].append(issue)
            print(f"  {issue}")
        
        # Log to audit
        reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
        reg.execute("UPDATE sites SET last_link_check_at=CURRENT_TIMESTAMP WHERE site_id=?", (site_id,))
        reg.execute("""INSERT INTO audit_log (site_id, check_type, status, issues_found)
            VALUES (?, 'link_check', ?, ?)""",
            (site_id, 'fail' if site_issues else 'pass', site_issues))
        reg.commit()
        reg.close()
        print(f"  {len(pages)} pages, {site_issues} issues\n")

    print(f"{'='*50}")
    print(f"Link Check complete: {total['pages']} pages, {total['links']} links")
    print(f"  Broken internal: {total['broken_internal']}")
    print(f"  Broken external: {total['broken_external']}")
    if not CHECK_EXTERNAL:
        print(f"  External links skipped (use --check-external to verify): {total['skipped_external']}")
    if total["skipped_viator"]:
        print(f"  Viator links skipped (verified by other flywheels): {total['skipped_viator']}")
    if total["issues"]:
        print(f"\nAll issues:")
        for i in total["issues"][:20]:
            print(f"  -> {i}")
        if len(total["issues"]) > 20:
            print(f"  ... and {len(total['issues'])-20} more")
    sys.exit(1 if total["broken_internal"] > 0 else 0)

if __name__ == "__main__":
    main()
