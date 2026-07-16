#!/usr/bin/env python3
"""
Template Honesty Audit — verify privacy/about claims match actual scripts loaded.
Catches false claims like "no tracking" on pages that load GA4/GoatCounter.

Usage: python3 template_audit.py <site_dir> [--fix]
Exit 0 = clean. Exit 1 = contradictions found.
"""

import os, re, sys

# Claims that deny ALL tracking/analytics (not category-specific truth)
# These are blanket denials that contradict analytics presence
TRACKING_DENIALS = [
    # Blanket: "we do not collect personal data/any data"
    r'does\s+not\s+collect\s+(any\s+)?(personal\s+)?(data|information)',
    r'never\s+collects?\s+(any\s+)?(personal\s+)?(data|information)',
    r'we\s+do\s+not\s+collect\s+(any\s+)?(personal\s+)?(data|information)',
    r'no\s+(personal\s+)?(data|information)\s+(is\s+)?collected',
    # Blanket: "no tracking whatsoever" (not "no tracking pixels")
    r'no\s+tracking\s+of\s+(any\s+kind|visitors|users|you)',
    r'(does|will)\s+not\s+track\s+(you|visitors|users|anyone)',
    r'we\s+do\s+n[o\']t\s+track\s+(you|visitors|users|anyone)',
    # Blanket: "no analytics" 
    r'(uses?\s+)?no\s+analytics',
    r'without\s+analytics',
    # Blanket: "we never share" (overstated — data goes to GA servers)
    r'never\s+shares?\s+(any\s+)?(data|information)\s+with',
    # Blanket: "this site does not collect anything" 
    r'this\s+site\s+does\s+not\s+collect',
    r'site\s+collects\s+almost\s+nothing',
]

ANALYTICS_SCRIPTS = [
    r'googletagmanager\.com',    # GA4
    r'gc\.zgo\.at',              # GoatCounter
    r'google-analytics\.com',    # Universal Analytics
    r'plausible\.io',            # Plausible
    r'fathom\.analytics',        # Fathom
]

UTILITY_PAGES = [
    'privacy', 'datenschutz', 'privacidad',
    'about', 'ueber-uns', 'acerca-de',
]


def find_utility_pages(site_dir):
    """Find privacy/about pages across languages."""
    pages = []
    for root, dirs, files in os.walk(site_dir):
        dirs[:] = [d for d in dirs if 'backup' not in d and not d.startswith('.')]
        for fn in files:
            if not fn.endswith('.html'):
                continue
            # Check if filename or parent dir matches a utility page
            name = fn.replace('.html', '')
            parent = os.path.basename(root)
            if name in UTILITY_PAGES or parent in UTILITY_PAGES:
                pages.append(os.path.join(root, fn))
    return pages


def audit_page(filepath):
    """Return list of contradictions found on this page."""
    with open(filepath) as f:
        content = f.read()

    # Extract body content (skip JSON-LD, scripts)
    body_match = re.search(r'<body[^>]*>(.*?)</body>', content, re.DOTALL)
    if not body_match:
        return []
    body = body_match.group(1)

    # Strip script/style blocks from body
    body_clean = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', body, flags=re.DOTALL)

    # Check for tracking denials
    denials_found = []
    for pattern in TRACKING_DENIALS:
        matches = re.findall(pattern, body_clean, re.IGNORECASE)
        if matches:
            denials_found.append(pattern)

    if not denials_found:
        return []  # No denial claims to audit

    # Check what analytics actually load
    analytics_found = []
    for pattern in ANALYTICS_SCRIPTS:
        if re.search(pattern, content, re.IGNORECASE):
            analytics_found.append(pattern)

    if not analytics_found:
        return []  # Claims no tracking AND genuinely has no analytics — honest

    # Contradiction: claims no tracking but analytics present
    contradictions = []
    for denial in denials_found:
        for analytic in analytics_found:
            contradictions.append(
                f"Claim '{denial}' contradicts loaded script '{analytic}'"
            )

    return contradictions


def main():
    if len(sys.argv) < 2:
        print("Usage: template_audit.py <site_dir> [--fix]")
        sys.exit(2)

    site_dir = os.path.abspath(sys.argv[1])
    fix_mode = '--fix' in sys.argv

    pages = find_utility_pages(site_dir)
    if not pages:
        print("No utility pages found")
        sys.exit(0)

    all_contradictions = {}
    for page in pages:
        rel = os.path.relpath(page, site_dir)
        issues = audit_page(page)
        if issues:
            all_contradictions[rel] = issues

    if not all_contradictions:
        print(f"Template honesty: clean ({len(pages)} pages audited)")
        sys.exit(0)

    print(f"TEMPLATE HONESTY FAILURES ({len(all_contradictions)} pages):")
    for page, issues in all_contradictions.items():
        print(f"  {page}:")
        for issue in issues:
            print(f"    - {issue}")

    if fix_mode:
        print("\nAuto-fix not implemented for template honesty.")
        print("Manual fix: rewrite the false claim in the page text to accurately")
        print("disclose what analytics/tracking is actually present.")
        print("Example: 'no tracking' → 'uses GA4 with IP anonymization for basic analytics'")

    sys.exit(1)


if __name__ == '__main__':
    main()
