#!/usr/bin/env python3
"""
Audit: Validate all Viator URLs across fleet sites for broken/fabricated patterns.

Checks:
  1. FABRICATED — product code doesn't match standard Viator format (NNNNNNPNNN)
  2. ALL_TOURS — URL contains the hallucinated "-All-Tours" suffix
  3. MISSING_DEST — bare /tours/CODE without /dDESTID-CODE prefix
  4. UNKNOWN_DB — code not found in viator_cli.db (requires synced DB)

Usage: python3 audit_broken_urls.py [--csv]
Output: site, file, url, product_code, issue_type
Exit 0 = clean, 1 = issues found.
"""
import os, re, sys, sqlite3, csv

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")

# Valid Viator product code pattern: digits+P+digits (e.g., 5516800P30, 424075P1)
VALID_CODE_PATTERN = re.compile(r'^\d+P\d+$')


def get_sites():
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    sites = reg.execute(
        "SELECT site_id, local_path FROM sites WHERE status='active'"
    ).fetchall()
    reg.close()
    return sites


def get_valid_codes():
    """Get all known product codes from viator_cli.db."""
    try:
        db = sqlite3.connect(os.path.join(DB_DIR, "viator_cli.db"))
        codes = set()
        for row in db.execute("SELECT product_code FROM products"):
            codes.add(row[0])
        db.close()
        return codes
    except Exception:
        return set()


def classify_url(url, valid_codes):
    """Classify a Viator URL into issue categories. Returns list of issue types."""
    issues = []

    # Extract product code — /dDESTID-CODE format
    # Viator URLs: /d22130-5516800P30 (with hyphen) OR /d280962P2 (no hyphen)
    # Capture both parts and combine for the full product code
    code_match = re.search(r'/d(\d+)-?(\w+)(?:[?/]|$)', url)
    if code_match:
        # Combine dest ID + suffix to get full product code (e.g., 280962 + P2 = 280962P2)
        product_code = code_match.group(1) + code_match.group(2)
    else:
        # Fallback: bare /tours/CODE or /tours/City/CODE
        code_match = re.search(r'/tours/(?:[^/]+/)*([^/?]+?)(?:[?/]|$)', url)
        product_code = code_match.group(1) if code_match else None

    # Skip destination-level URLs (ttd, no product code)
    if product_code and (product_code.lower() == 'ttd' or product_code.lower().endswith('ttd')):
        return issues, None  # Destination page — not a product URL

    # 1. Check for All-Tours fabrication
    if '-All-Tours' in url or product_code == 'All-Tours':
        issues.append('ALL_TOURS')
        return issues, product_code

    # 2. Check for fabricated code pattern
    # Truly fabricated: digits followed by full CAPS word (e.g., 9012FLOAT, 5678ICE, 1234FADOPORTO)
    if product_code and re.match(r'^\d{3,4}[A-Z]{3,}', product_code):
        issues.append('FABRICATED')
    # Numbers-only codes (old Viator format) are NOT fabricated — they're UNKNOWN_DB
    elif product_code and re.match(r'^\d+$', product_code):
        pass  # Legacy numeric code — classify via DB check only
    # Standard pattern codes that don't match NNNNNNPNNN
    elif product_code and not VALID_CODE_PATTERN.match(product_code):
        # Could be city name mistakenly extracted — check if it looks like a word
        if re.match(r'^[A-Z][a-z]', product_code):
            pass  # Likely a city name, not a product code — skip classification
        else:
            issues.append('UNUSUAL_FORMAT')

    # 3. Check for missing destination prefix
    # A proper Viator product URL has /tours/region/slug/dDESTID-CODE or /dDESTID-CODE
    has_dest_prefix = bool(re.search(r'/d\d+-?', url))
    has_tours_path = '/tours/' in url
    if product_code and VALID_CODE_PATTERN.match(product_code):
        if not has_dest_prefix and has_tours_path:
            # Bare /tours/CODE without /dDESTID prefix
            issues.append('MISSING_DEST')

    # 4. Check against known codes in DB
    if product_code and (VALID_CODE_PATTERN.match(product_code) or re.match(r'^\d+$', product_code)):
        if valid_codes and product_code not in valid_codes:
            issues.append('UNKNOWN_DB')

    return issues, product_code


def audit_site(site_id, site_path, valid_codes):
    """Scan all HTML files for broken Viator URLs."""
    findings = []

    for root, dirs, files in os.walk(site_path):
        SKIP_DIRS = {'backup', 'backup_pre_fixes', 'backup_fresh',
                     'css', 'images', '.git', 'node_modules'}
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS
                   and not d.startswith('.')
                   and not d.startswith('backup_nav_')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            fpath = os.path.join(root, fn)
            rel = os.path.relpath(fpath, site_path)
            try:
                with open(fpath) as f:
                    html = f.read()
            except Exception:
                continue

            # Skip JSON-LD blocks when extracting URLs
            visible = re.sub(
                r'<script[^>]*type="application/ld\+json"[^>]*>.*?</script>',
                '', html, flags=re.DOTALL
            )

            # Extract all Viator URLs
            for url_match in re.finditer(
                r'href="(https?://(?:www\.)?viator\.com/[^"]*)"',
                visible
            ):
                url = url_match.group(1)
                issues, code = classify_url(url, valid_codes)

                for issue_type in issues:
                    findings.append({
                        'site': site_id,
                        'file': rel,
                        'url': url,
                        'product_code': code or 'N/A',
                        'issue_type': issue_type,
                    })

    return findings


def main():
    csv_mode = '--csv' in sys.argv

    sites = get_sites()
    valid_codes = get_valid_codes()
    print(f"Known product codes in DB: {len(valid_codes)}")

    all_findings = []
    for site_id, site_path in sites:
        findings = audit_site(site_id, site_path, valid_codes)
        all_findings.extend(findings)

    if csv_mode:
        writer = csv.DictWriter(
            sys.stdout,
            fieldnames=['site', 'file', 'url', 'product_code', 'issue_type']
        )
        writer.writeheader()
        for f in sorted(all_findings, key=lambda x: (x['issue_type'], x['site'], x['file'])):
            writer.writerow(f)
    else:
        if not all_findings:
            print("✅ All Viator URLs valid — no broken patterns found")
        else:
            # Group by issue type
            by_type = {}
            for f in all_findings:
                by_type.setdefault(f['issue_type'], []).append(f)

            for issue_type, items in sorted(by_type.items()):
                print(f"\n{'='*60}")
                print(f"  {issue_type}: {len(items)} occurrence(s)")
                print(f"{'='*60}")
                for item in items:
                    print(f"  [{item['site']}] {item['file']}")
                    print(f"       code={item['product_code']}")
                    print(f"       url={item['url'][:120]}")
                    print()

    return 1 if all_findings else 0


if __name__ == '__main__':
    sys.exit(main())
