#!/usr/bin/env python3
"""
Sitemap health check. Verifies every sitemap URL returns 200 on live domain.
Usage: python3 sitemap_check.py [all|site_id]
"""
import sqlite3, os, sys, re, urllib.request, urllib.error, xml.etree.ElementTree as ET
from urllib.parse import urlparse
from datetime import datetime
class _RedirectHandler308(urllib.request.HTTPRedirectHandler):
    """Handle 308 Permanent Redirect (missing from Python 3.9's stdlib)."""
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed = (301, 302, 303, 307, 308)
        if code not in allowed:
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        m = req.get_method()
        if not ((code in allowed and m in ("GET", "HEAD")) or
                (code in (301, 302, 303) and m == "POST")):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        newurl = newurl.replace(" ", "%20")
        CONTENT_HEADERS = ("content-length", "content-type")
        newheaders = {k: v for k, v in req.headers.items()
                      if k.lower() not in CONTENT_HEADERS}
        return urllib.request.Request(newurl, headers=newheaders,
                                      origin_req_host=req.origin_req_host,
                                      unverifiable=True)

def _build_opener():
    opener = urllib.request.build_opener(_RedirectHandler308)
    opener.addheaders = [("User-Agent", "Hermes-SitemapCheck/1.0")]
    return opener



DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")

def get_sites(target="all"):
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute("SELECT site_id, domain FROM sites WHERE status='active'").fetchall()
    else:
        sites = reg.execute("SELECT site_id, domain FROM sites WHERE site_id=? AND status='active'", (target,)).fetchall()
    reg.close()
    return sites

def fetch_sitemap_urls(domain):
    sitemap_url = f"https://{domain}/sitemap.xml"
    try:
        req = urllib.request.Request(sitemap_url, headers={"User-Agent": "Hermes-SitemapCheck/1.0"})
        resp = _build_opener().open(req, timeout=10)
        xml = resp.read().decode()
        root = ET.fromstring(xml)
        ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = [loc.text for loc in root.findall(".//ns:loc", ns)]
        return urls
    except Exception as e:
        return []

def check_url(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Hermes-SitemapCheck/1.0"})
        resp = _build_opener().open(req, timeout=10)
        return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        print(f"  [WARN] URL check failed: {e}", file=sys.stderr)
        return 0

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"Sitemap Check — {target} — {datetime.now().isoformat()[:19]}\n")

    sites = get_sites(target)
    results = {"passed": 0, "failed": 0, "issues": []}

    for site_id, domain in sites:
        print(f"--- {site_id} ({domain}) ---")
        urls = fetch_sitemap_urls(domain)
        if not urls:
            print(f"  ERROR: Could not fetch sitemap.xml")
            results["failed"] += 1
            continue

        site_issues = 0
        for url in urls:
            # Cross-domain check: URL's domain must match the site's domain
            parsed = urlparse(url)
            url_domain = parsed.netloc.lower()
            # Strip leading www. for comparison
            url_domain_clean = url_domain.removeprefix("www.")
            site_domain_clean = domain.removeprefix("www.")
            if url_domain_clean != site_domain_clean:
                site_issues += 1
                print(f"  FAIL CROSS-DOMAIN: {url} (expected domain: {domain})")
                results["issues"].append(f"{site_id}: CROSS-DOMAIN {url} (expected {domain})")
                continue  # Don't try to fetch a URL on a different domain
            status = check_url(url)
            if status != 200:
                site_issues += 1
                print(f"  FAIL {status}: {url}")
                results["issues"].append(f"{site_id}: {status} {url}")
            # Don't print every pass - too noisy

        if site_issues == 0:
            results["passed"] += 1
            print(f"  ALL {len(urls)} URLs return 200")
            # Update site timestamps in DB
            try:
                reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
                reg.execute("UPDATE sites SET last_sitemap_check=CURRENT_TIMESTAMP WHERE site_id=?", (site_id,))
                reg.execute("INSERT INTO audit_log (site_id, check_type, status, details) VALUES (?, 'sitemap_check', 'pass', 'All URLs OK — ' || ? || ' urls')",
                           (site_id, str(len(urls))))
                reg.commit()
                reg.close()
            except Exception as e:
                print(f"  [WARN] DB update failed: {e}", file=sys.stderr)
        else:
            results["failed"] += 1
            print(f"  {site_issues}/{len(urls)} URLs failed")
            try:
                reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
                reg.execute("UPDATE sites SET last_sitemap_check=CURRENT_TIMESTAMP WHERE site_id=?", (site_id,))
                reg.execute("INSERT INTO audit_log (site_id, check_type, status, issues_found, details) VALUES (?, 'sitemap_check', 'fail', ?, ? || ' URLs failed')",
                           (site_id, site_issues, str(site_issues)))
                reg.commit()
                reg.close()
            except Exception as e:
                print(f"  [WARN] DB update failed: {e}", file=sys.stderr)

    print(f"\n{'='*50}")
    print(f"Sitemap complete: {results['passed']} passed, {results['failed']} failed")
    if results["issues"]:
        print(f"\nFailed URLs:")
        for i in results["issues"]:
            print(f"  -> {i}")
    sys.exit(1 if results["failed"] > 0 else 0)

if __name__ == "__main__":
    main()
