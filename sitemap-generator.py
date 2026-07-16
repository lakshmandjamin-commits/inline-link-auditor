#!/usr/bin/env python3
"""
sitemap-generator.py — Generate sitemap.xml with xhtml:link hreflang alternates

Usage:
    python3 sitemap-generator.py --dir /path/to/site --domain example.com [--output sitemap.xml]
    python3 sitemap-generator.py --dir . --domain example.com  # writes to ./sitemap.xml

Reads all HTML files, extracts hreflang tags from each, and produces a complete
sitemap.xml with xhtml:link alternates for every URL.

Features:
  - Finds all HTML files (excluding backup/node_modules/.git)
  - Extracts hreflang from each page's <head>
  - Generates <url> entries with <loc>, <lastmod> (from file mtime), and <xhtml:link> for each language variant
  - Sets appropriate <priority> based on page type (homepage=1.0, hubs=0.9, content=0.7, utility=0.4)
  - Validates output XML
  - Google/Bing ping endpoints deprecated June 2023 — use <lastmod> + robots.txt instead
"""

import argparse
import os
import re
import sys
from xml.dom import minidom
from xml.sax.saxutils import escape


def find_html_files(directory):
    """Find all HTML files excluding backup/node_modules/.git dirs."""
    html_files = []
    for root, dirs, files in os.walk(directory):
        rel_root = os.path.relpath(root, directory)
        if rel_root.split(os.sep)[0] in ('node_modules', '.git', 'backups'):
            dirs.clear()
            continue
        if any(seg.startswith('backup') for seg in rel_root.split(os.sep)):
            continue
        for f in files:
            if f.endswith('.html'):
                html_files.append(os.path.join(root, f))
    return sorted(html_files)


def extract_hreflangs(filepath, domain=None):
    """Extract (canonical, {lang: href}) from an HTML file.
    If domain is provided, hreflang URLs from other domains are filtered out."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # Canonical — order-agnostic (href may appear before or after rel)
    canonical = None
    m = re.search(r'<link[^>]*rel="canonical"[^>]*href="([^"]*)"', content)
    if not m:
        m = re.search(r'<link[^>]*href="([^"]*)"[^>]*rel="canonical"', content)
    if m:
        canonical = m.group(1)

    # Enforce www. prefix on all canonical URLs
    if canonical and 'www.' not in canonical and 'localhost' not in canonical:
        canonical = canonical.replace('https://', 'https://www.')
        canonical = canonical.replace('http://', 'http://www.')

    # Strip trailing slashes from path canonicals (preserve root domain /)
    # https://domain.com/about/ → https://domain.com/about
    # https://domain.com/ stays as-is (root trailing slash is correct)
    if canonical and canonical.endswith('/') and canonical.count('/') > 3:
        canonical = canonical.rstrip('/')

    # Hreflangs — try both attribute orders
    hreflangs = re.findall(
        r'<link[^>]*rel="alternate"[^>]*hreflang="([\w-]+)"[^>]*href="([^"]*)"',
        content
    )
    # If none found, try reversed order (href before hreflang)
    if not hreflangs:
        raw = re.findall(
            r'<link[^>]*href="([^"]*)"[^>]*rel="alternate"[^>]*hreflang="([\w-]+)"',
            content
        )
        hreflangs = [(lang, href) for href, lang in raw]
    hreflang_map = {}
    for lang, href in hreflangs:
        # DOMAIN VALIDATION: reject hreflang URLs that don't match the target domain
        if domain and domain not in href:
            continue
        hreflang_map[lang] = href

    return canonical, hreflang_map


def determine_priority(url_path):
    """Determine sitemap priority based on URL path."""
    if url_path in ('', '/'):
        return '1.0'
    if url_path in ('/de', '/es', '/de/', '/es/'):
        return '0.9'

    # Strip leading/trailing slash
    clean = url_path.strip('/')

    # Utility pages
    utility_pages = {'about', 'contact', 'faq'}
    low_priority = {'privacy', 'privacy-policy', 'terms', 'disclaimer', 'cookies'}
    if clean in low_priority:
        return '0.3'
    if clean in utility_pages:
        return '0.6'

    # Category hubs (single-segment pages without hyphens or with common names)
    hubs = {
        'hiking', 'teide', 'whale-watching', 'boat-tours', 'stargazing',
        'snorkeling', 'adventure', 'food-wine',
    }
    if clean in hubs:
        return '0.9'

    # Language subdirectory hubs
    de_es_hubs = {
        'wanderungen', 'teide-wanderungen', 'walbeobachtung', 'bootsausfluege',
        'sternbeobachtung', 'schnorcheln', 'abenteuer', 'essen-wein',
        'senderismo', 'senderismo-teide', 'avistamiento-de-cetaceos',
        'paseos-en-barco', 'observacion-de-estrellas', 'buceo',
        'aventura', 'comida-vino',
    }
    if clean in de_es_hubs:
        return '0.9'

    # Everything else is content (0.7)
    return '0.7'


def generate_sitemap(html_files, domain):
    """Generate sitemap XML string from HTML file data."""
    entries = []
    seen_locs = set()

    for fp in html_files:
        canonical, hreflangs = extract_hreflangs(fp, domain)
        if not canonical:
            continue

        # DOMAIN VALIDATION: skip canonical URLs that don't match the target domain
        if domain not in canonical:
            continue

        # Deduplicate: skip if we've already added this URL
        if canonical in seen_locs:
            continue
        seen_locs.add(canonical)

        # Extract URL path from canonical for priority
        url_path = re.sub(r'^https?://[^/]+', '', canonical)
        priority = determine_priority(url_path)

        # Get lastmod from file modification time
        lastmod = ''
        try:
            mtime = os.path.getmtime(fp)
            from datetime import datetime
            lastmod = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')
        except OSError:
            pass

        # Build entry
        entry = f'  <url>\n    <loc>{escape(canonical)}</loc>\n'
        if lastmod:
            entry += f'    <lastmod>{lastmod}</lastmod>\n'

        # Add xhtml:link alternates from hreflangs
        for lang in sorted(hreflangs.keys()):
            href = hreflangs[lang]
            # Strip trailing slashes from hreflang URLs (matches canonical behavior)
            if href.endswith('/') and href.count('/') > 3:
                href = href.rstrip('/')
            entry += (
                f'    <xhtml:link rel="alternate"'
                f' hreflang="{lang}" href="{escape(href)}"/>\\n'
            )

        entry += f'    <priority>{priority}</priority>\n  </url>'
        entries.append(entry)

    # Build full XML
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"',
        '        xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    ]
    xml_parts.extend(entries)
    xml_parts.append('</urlset>')

    return '\n'.join(xml_parts) + '\n'


def validate_xml(xml_str):
    """Validate XML is well-formed."""
    try:
        minidom.parseString(xml_str)
        return True, None
    except Exception as e:
        return False, str(e)


def validate_domain_gate(xml_str, domain):
    """Hard gate: assert every <loc> URL matches the target domain.
    Returns (passed, failures) where failures is a list of (url, expected_domain)."""
    import re as _re
    failures = []
    locs = _re.findall(r'<loc>(https?://[^<]+)</loc>', xml_str)
    for loc in locs:
        # Extract domain from loc URL
        loc_domain = _re.sub(r'^https?://(?:www\.)?', '', loc).split('/')[0]
        if loc_domain != domain.replace('www.', ''):
            failures.append((loc, domain))
    return len(failures) == 0, failures


def main():
    parser = argparse.ArgumentParser(description='Generate sitemap.xml with hreflang')
    parser.add_argument('--dir', default='.', help='Site root directory (default: .)')
    parser.add_argument('--domain', required=True, help='Canonical domain (e.g., example.com)')
    parser.add_argument('--output', '-o', default='sitemap.xml',
                        help='Output file path (default: sitemap.xml in --dir)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print sitemap to stdout instead of writing')
    args = parser.parse_args()

    site_dir = os.path.abspath(args.dir)
    if not os.path.isdir(site_dir):
        print(f"ERROR: Directory not found: {site_dir}", file=sys.stderr)
        sys.exit(1)

    html_files = find_html_files(site_dir)
    if not html_files:
        print(f"ERROR: No HTML files found in {site_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {len(html_files)} HTML files in {site_dir}", file=sys.stderr)
    print(f"Domain: {args.domain}", file=sys.stderr)

    xml = generate_sitemap(html_files, args.domain)
    url_count = xml.count('<url>')

    # Validate
    valid, err = validate_xml(xml)
    if not valid:
        print(f"ERROR: Generated XML is invalid: {err}", file=sys.stderr)
        sys.exit(1)

    hreflang_count = xml.count('<xhtml:link')
    print(f"Generated sitemap: {url_count} URLs, {hreflang_count} xhtml:link entries",
          file=sys.stderr)
    print(f"XML validation: PASSED", file=sys.stderr)

    # ── DOMAIN VALIDATION GATE (Phase 3: hard gate at generation time) ──
    domain_ok, domain_failures = validate_domain_gate(xml, args.domain)
    if not domain_ok:
        print(f"ERROR: {len(domain_failures)} URL(s) in sitemap don't match domain '{args.domain}':",
              file=sys.stderr)
        for url, expected in domain_failures[:10]:
            print(f"  {url} (expected: {expected})", file=sys.stderr)
        print("", file=sys.stderr)
        print("SITEMAP DOMAIN GATE FAILED — fix the generator or source pages before deploying.",
              file=sys.stderr)
        sys.exit(1)
    print(f"Domain validation: PASSED ({url_count} URLs match {args.domain})", file=sys.stderr)

    if args.dry_run:
        print(xml)
    else:
        output_path = args.output
        if not os.path.isabs(output_path):
            output_path = os.path.join(site_dir, output_path)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(xml)

        file_size = os.path.getsize(output_path)
        print(f"Written: {output_path} ({file_size:,} bytes)", file=sys.stderr)

    # Summary to stdout (important for calling scripts)
    print(f"URLS:{url_count} HREFLANGS:{hreflang_count} FILE:{args.output}")


if __name__ == '__main__':
    main()
