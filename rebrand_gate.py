#!/usr/bin/env python3
"""
Rebrand Gate — verifies site CSS doesn't contain leftover template brand strings.
Yogya visual upgrade lesson: CSS header said "Porto Wine Tours — Design System v3".

Usage: python3 rebrand_gate.py <site_dir>
Exit: 0=pass, 1=fail (wrong brand found)
"""
import re, sys
from pathlib import Path

# Known template brand strings that should never appear in a live site
WRONG_BRANDS = [
    "Porto Wine Tours",
    "porto-sommelier",
    "Madeira Trail Guide",
    "madeira-trail-guide",
    "Tenerife Outdoor",
    "tenerife-outdoor",
    "Lapland Adventure",
    "lapland-adventure",
]


def check_site(site_dir):
    site = Path(site_dir)
    css = site / 'css' / 'style.css'
    if not css.exists():
        print(f"ERROR: {css} not found")
        return 1

    content = css.read_text()
    failures = []
    
    for brand in WRONG_BRANDS:
        if brand.lower() in content.lower():
            # Find which lines
            for line_num, line in enumerate(content.split('\n'), 1):
                if brand.lower() in line.lower():
                    failures.append((brand, line_num, line.strip()[:100]))
    
    if failures:
        print(f"❌ REBRAND GATE FAILED — found {len(failures)} wrong brand reference(s):")
        for brand, line_num, line in failures:
            print(f"  Line {line_num}: '{brand}' — {line}")
        print(f"\n❌ REBRAND GATE FAILED")
        return 1
    
    # Also verify the CSS has a proper header comment
    first_lines = '\n'.join(content.split('\n')[:5])
    if 'Design System' not in first_lines:
        print("⚠️  WARNING: No 'Design System' header found in CSS — may be untracked")
    else:
        print(f"✅ REBRAND GATE PASSED — CSS header identifies this site's design system")
    
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: rebrand_gate.py <site_dir>")
        sys.exit(1)
    sys.exit(check_site(sys.argv[1]))
