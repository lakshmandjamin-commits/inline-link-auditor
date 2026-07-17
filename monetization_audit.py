#!/usr/bin/env python3
"""
Monetization Audit — Hard gate for affiliate site builds.

Checks every HTML page in a site directory and blocks deploy if:
  1. Homepage has fewer than 5 product cards AND a category grid
  2. Any editorial page has zero Viator links in its first 400 words
  3. Any page has zero Viator links total (except explicit utility pages)

Exit 0 = PASS. Exit 1 = FAIL (BLOCKS DEPLOY).

Usage:
  python3 monetization_audit.py <site_dir> [--json]
  python3 monetization_audit.py yogyakarta-temple-tours
"""

import sys
import os
import re
import glob
import json
from pathlib import Path

UTILITY_PAGES = {'contact.html', 'privacy.html', '404.html', 'terms.html'}


def first_n_words_has_viator(html: str, n: int = 400) -> bool:
    """Check if a Viator link exists within the first N words of <main> body text."""
    main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
    body_html = main_match.group(1) if main_match else html

    # Strip product-card sections (they're not "inline narrative" links)
    # Remove everything between <div class="product-card"> and its matching close
    while True:
        start = body_html.find('<div class="product-card">')
        if start == -1:
            break
        # Find matching </div> by counting depth
        depth = 1
        pos = start + len('<div class="product-card">')
        while depth > 0 and pos < len(body_html):
            next_open = body_html.find('<div', pos)
            next_close = body_html.find('</div>', pos)
            if next_close == -1:
                break
            if next_open != -1 and next_open < next_close:
                depth += 1
                pos = next_open + 4
            else:
                depth -= 1
                pos = next_close + 6
        body_html = body_html[:start] + body_html[pos:]
    # Strip all HTML tags, count words
    text = re.sub(r'<[^>]+>', ' ', body_html)
    words = text.split()

    # Walk through first N words, check if any viator.com link appears
    word_count = 0
    in_link = False
    link_href = ''

    for token in re.split(r'(<a\s[^>]*>|</a>)', body_html):
        if token.startswith('<a ') and 'viator.com' in token:
            in_link = True
            # Extract href for word-count approximation
            href_m = re.search(r'href="([^"]*viator\.com[^"]*)"', token)
            if href_m:
                link_href = href_m.group(1)
        elif token == '</a>':
            in_link = False
        elif not token.startswith('<'):
            token_words = token.split()
            word_count += len(token_words)
            if in_link and word_count <= n:
                return True
            if word_count > n:
                break

    return False


def count_product_cards(html: str) -> int:
    """Count product-card divs on the page."""
    return len(re.findall(r'class="product-card"', html))


def has_category_grid(html: str) -> bool:
    """Check if page has a category-grid section."""
    return 'class="category-grid"' in html or 'class="categories"' in html


def count_viator_links(html: str) -> int:
    """Count total viator.com links in the page."""
    return len(re.findall(r'viator\.com', html))


def is_editorial_page(path: str, html: str) -> bool:
    """Determine if a page is editorial (should have Viator links)."""
    basename = os.path.basename(path)
    if basename in UTILITY_PAGES:
        return False
    # Pages with product-card or category sections are editorial
    if 'class="product-card"' in html or 'class="categories"' in html:
        return True
    # Pages with travel/tour editorial content
    if '<main' in html and re.search(r'<main[^>]*>', html):
        body = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
        if body and len(body.group(1).split()) > 300:
            return True
    return False


def audit(site_dir: str) -> dict:
    """Run full monetization audit. Returns dict with results."""
    results = {
        'pass': True,
        'pages': {},
        'failures': [],
        'homepage_cards': 0,
        'homepage_has_grid': False,
    }

    for html_file in sorted(glob.glob(f'{site_dir}/**/*.html', recursive=True)):
        rel = os.path.relpath(html_file, site_dir)
        html = open(html_file, encoding='utf-8', errors='ignore').read()

        viator_count = count_viator_links(html)
        editorial = is_editorial_page(html_file, html)
        is_homepage = rel == 'index.html'
        has_inline = first_n_words_has_viator(html) if editorial else None
        cards = count_product_cards(html)

        page_result = {
            'viator_links': viator_count,
            'editorial': editorial,
            'inline_first_400': has_inline,
            'product_cards': cards,
        }

        if is_homepage:
            results['homepage_cards'] = cards
            results['homepage_has_grid'] = has_category_grid(html)

        # Gate checks
        failures = []

        if is_homepage:
            if cards < 5:
                failures.append(f'Homepage has {cards} product cards (need ≥5)')
            if not has_category_grid(html):
                failures.append('Homepage missing category grid')

        if editorial and not is_homepage:
            if viator_count == 0:
                failures.append(f'Zero Viator links on editorial page')
            if has_inline is False:
                failures.append('No inline Viator link in first 400 words')

        if failures:
            page_result['failures'] = failures
            results['failures'].append({'page': rel, 'issues': failures})
            results['pass'] = False

        results['pages'][rel] = page_result

    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: monetization_audit.py <site_dir> [--json]", file=sys.stderr)
        sys.exit(2)

    site_dir = sys.argv[1]
    if not os.path.isdir(site_dir):
        print(f"ERROR: {site_dir} is not a directory", file=sys.stderr)
        sys.exit(2)

    results = audit(site_dir)
    json_out = '--json' in sys.argv

    if json_out:
        print(json.dumps(results, indent=2))
    else:
        # Human-readable output
        print("=" * 60)
        print("MONETIZATION AUDIT")
        print("=" * 60)
        print(f"\nHomepage: {results['homepage_cards']} product cards, "
              f"grid={'✓' if results['homepage_has_grid'] else '✗'}")
        print(f"\nPer-page Viator links:")
        for page, data in sorted(results['pages'].items()):
            inline = '✓' if data.get('inline_first_400') else ('✗' if data.get('inline_first_400') is False else '-')
            status = 'FAIL' if data.get('failures') else 'PASS'
            print(f"  {status:4s}  {data['viator_links']:2d} links  inline:{inline}  {page}")
            if data.get('failures'):
                for f in data['failures']:
                    print(f"         → {f}")

        if results['pass']:
            print(f"\n✅ ALL CHECKS PASSED")
        else:
            print(f"\n❌ {len(results['failures'])} PAGE(S) FAILED — deploy blocked")

    if not results['pass']:
        sys.exit(1)


if __name__ == '__main__':
    main()
