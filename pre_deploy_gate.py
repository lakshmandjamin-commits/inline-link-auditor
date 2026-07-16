#!/usr/bin/env python3
"""
Pre-Deploy Gate — blocks deploys with broken/fabricated Viator URLs.

Run before pushing to GitHub or deploying to Vercel.
  python3 pre_deploy_gate.py /path/to/site
  python3 pre_deploy_gate.py --all    # all fleet sites

Exit 0 = clean, 1 = blocked (fix before deploy).

Checks:
  1. FABRICATED codes    — hallucinated product codes (e.g., 9012FLOAT)
  2. HERO links          — Viator <a> inside hero/header sections
  3. TRUST-ZONE links    — Viator <a> inside trust/author/about sections
  4. MISSING_DEST        — bare /tours/CODE without /dDESTID prefix
  5. BROKEN placeholders — codes like "?", "ALL_TOURS", "12345"
  6. PLACEHOLDER codes   — 12345, 67890, etc.
  7. RATING gate         — WARN on products with rating < 4.0 (needs synced DB)
"""

import os, re, sys, sqlite3, json

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'db')
SITES_DIR = os.path.expanduser('~/sites')

# ── Patterns ────────────────────────────────────────────────────────────────
VALID_CODE = re.compile(r'^\d+P\d+$')
FABRICATED_RE = re.compile(r'^\d{3,4}[A-Z]{3,}')
PLACEHOLDER_CODES = {'12345', '67890', '00000', '99999'}
HERO_PATTERNS = [
    r'<div[^>]*class="[^"]*hero[^"]*"[^>]*>',
    r'<header[^>]*>',
    r'<section[^>]*class="[^"]*hero[^"]*"[^>]*>',
]
TRUST_PATTERNS = [
    r'<div[^>]*class="[^"]*why-i-built[^"]*"[^>]*>',
    r'<div[^>]*class="[^"]*trust[^"]*"[^>]*>',
    r'<div[^>]*class="[^"]*author-box[^"]*"[^>]*>',
    r'<div[^>]*class="[^"]*about-author[^"]*"[^>]*>',
    r'<section[^>]*class="[^"]*author[^"]*"[^>]*>',
]


def get_ratings():
    """Get {product_code: rating} from viator_cli.db."""
    try:
        db_path = os.path.join(DB_DIR, 'viator_cli.db')
        db = sqlite3.connect(db_path)
        rows = db.execute(
            "SELECT product_code, rating FROM products WHERE rating IS NOT NULL"
        ).fetchall()
        db.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def extract_product_code(url):
    """Extract product code from a Viator URL. Returns None if not found."""
    # /dDESTID-CODE format (city pages)
    m = re.search(r'/d\d+-?(\w+)(?:[?/]|$)', url)
    if m:
        return m.group(1)
    # /tours/CODE format
    m = re.search(r'/tours/(\w+)(?:[?/]|$)', url)
    if m:
        return m.group(1)
    return None


def is_inside_excluded(body, pos, patterns):
    """Check if position 'pos' is inside any of the excluded section patterns.
    
    Strategy: find the last opening of each pattern before pos, then check
    if the corresponding closing tag is AFTER pos (meaning we're inside).
    """
    before = body[:pos]
    
    for pat in patterns:
        # Find the last occurrence of this pattern before pos
        last_open = None
        for m in re.finditer(pat, before):
            last_open = m
        if last_open is None:
            continue
        
        # Determine the closing tag for this section
        # For <header> → </header>, for <div> → </div>, for <section> → </section>
        tag_match = re.match(r'<(header|div|section)', pat)
        if not tag_match:
            continue
        tag = tag_match.group(1)
        
        # Check if the closing tag appears AFTER pos in the body
        # But BEFORE any other opening of the same tag type (nested sections)
        after_part = body[last_open.end():]
        # Find the matching close — count nesting depth
        depth = 1
        for inner_match in re.finditer(f'<{tag}[^>]*>|</{tag}>', after_part):
            if inner_match.group().startswith(f'</'):
                depth -= 1
                if depth == 0:
                    # Found the closing tag
                    close_pos = last_open.end() + inner_match.end()
                    if close_pos > pos:
                        return True  # URL is inside this section
                    break
            else:
                depth += 1
    
    return False


def gate_site(site_path):
    """Run all gates on a site. Returns findings dict."""
    ratings = get_ratings()
    findings = {
        'fabricated': [],
        'hero_links': [],
        'trust_links': [],
        'missing_dest': [],
        'broken_placeholder': [],
        'placeholder': [],
        'low_rating': [],
    }

    if not os.path.isdir(site_path):
        return {'error': f'Not a directory: {site_path}'}, 1

    site_name = os.path.basename(site_path)

    for root, dirs, files in os.walk(site_path):
        SKIP = {'.git', 'node_modules', 'css', 'images', 'backup'}
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('.') and not d.startswith('backup_nav_')]

        for fn in files:
            if not fn.endswith('.html'):
                continue
            fpath = os.path.join(root, fn)
            rel = os.path.relpath(fpath, site_path)

            try:
                with open(fpath, 'r') as f:
                    html = f.read()
            except Exception:
                continue

            # Extract all Viator URLs
            viator_urls = re.findall(
                r'https?://(?:www\.)?viator\.com/[^\s"\']+',
                html
            )

            for url in viator_urls:
                code = extract_product_code(url)
                if not code or code.lower() == 'ttd':
                    continue

                # Find position of this URL in the HTML
                url_pos = html.find(url)
                if url_pos == -1:
                    continue

                # 1. FABRICATED codes
                if FABRICATED_RE.match(code):
                    findings['fabricated'].append({
                        'file': rel, 'code': code, 'url_short': url[:80]
                    })

                # 2. PLACEHOLDER codes
                if code in PLACEHOLDER_CODES:
                    findings['placeholder'].append({
                        'file': rel, 'code': code
                    })

                # 3. BROKEN placeholders (single char or "ALL_TOURS" etc.)
                if code in ('?', 'ALL_TOURS', 'placeholder', 'CODE'):
                    findings['broken_placeholder'].append({
                        'file': rel, 'code': code, 'url_short': url[:80]
                    })

                # 4. MISSING_DEST — bare /tours/CODE without /dDESTID
                if VALID_CODE.match(code) and '/tours/' in url:
                    if not re.search(r'/d\d+-', url):
                        findings['missing_dest'].append({
                            'file': rel, 'code': code, 'url_short': url[:80]
                        })

                # 5. HERO links — Viator URL inside hero/header section
                if is_inside_excluded(html, url_pos, HERO_PATTERNS):
                    findings['hero_links'].append({
                        'file': rel, 'code': code, 'url_short': url[:80]
                    })

                # 6. TRUST-ZONE links — Viator URL inside trust/author section
                if is_inside_excluded(html, url_pos, TRUST_PATTERNS):
                    findings['trust_links'].append({
                        'file': rel, 'code': code, 'url_short': url[:80]
                    })

                # 7. LOW RATING warning
                if code in ratings and ratings[code] is not None:
                    rating = float(ratings[code])
                    if rating < 4.0:
                        findings['low_rating'].append({
                            'file': rel, 'code': code, 'rating': rating
                        })

    return findings, site_name


def print_findings(findings, site_name):
    """Print findings in a structured format."""
    blocked = False
    total = sum(len(v) for v in findings.values())

    if total == 0:
        print(f'✅ {site_name}: CLEAN — no Viator URL issues found')
        return False

    print(f'\n━━━ {site_name} ━━━')

    sections = [
        ('🔴 FABRICATED codes', 'fabricated'),
        ('🔴 PLACEHOLDER codes', 'placeholder'),
        ('🔴 BROKEN placeholders', 'broken_placeholder'),
        ('🟡 MISSING dest prefix', 'missing_dest'),
        ('🟠 HERO section links', 'hero_links'),
        ('🟠 TRUST-ZONE links', 'trust_links'),
        ('🔵 LOW RATING (<4.0★)', 'low_rating'),
    ]

    for label, key in sections:
        items = findings[key]
        if not items:
            continue
        if key in ('fabricated', 'placeholder', 'broken_placeholder',
                    'hero_links', 'trust_links'):
            blocked = True

        print(f'\n  {label} ({len(items)}):')
        for item in items[:10]:
            extra = ''
            if 'rating' in item:
                extra = f' [{item["rating"]}★]'
            print(f'    {item["file"]}')
            print(f'      → {item.get("url_short", item.get("code","?"))}{extra}')
        if len(items) > 10:
            print(f'    ... and {len(items) - 10} more')

    return blocked


def main():
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        sys.exit(1)

    if '--all' in args:
        sites = []
        if os.path.isdir(SITES_DIR):
            for d in os.listdir(SITES_DIR):
                full = os.path.join(SITES_DIR, d)
                if os.path.isdir(full) and os.path.isdir(os.path.join(full, '.git')):
                    sites.append(full)
        if not sites:
            print('No sites found in ~/sites/')
            sys.exit(1)
    else:
        sites = [os.path.abspath(a) for a in args if not a.startswith('--')]

    any_blocked = False
    for site_path in sites:
        result = gate_site(site_path)
        if isinstance(result, dict) and 'error' in result:
            print(f'❌ {result["error"]}')
            any_blocked = True
            continue
        findings, site_name = result
        blocked = print_findings(findings, site_name)
        if blocked:
            any_blocked = True

        # Canonical check (www + trailing-slash)
        verify_script = os.path.join(os.path.dirname(__file__), 'verify_canonical_www.py')
        if os.path.exists(verify_script):
            import subprocess
            canon_result = subprocess.run(
                ['python3', verify_script, '--dir', site_path],
                capture_output=True, text=True, timeout=30
            )
            if canon_result.returncode != 0:
                print(f'\n  CANONICAL ISSUES ({site_name}):')
                print(canon_result.stdout.strip())
                any_blocked = True
            else:
                print(f'  canonicals clean: {site_name}')

    if any_blocked:
        print('\n🚫 DEPLOY BLOCKED — fix the issues above before deploying.')
        sys.exit(1)
    else:
        print('\n✅ ALL CLEAR — safe to deploy.')
        sys.exit(0)


if __name__ == '__main__':
    main()
