#!/usr/bin/env python3
"""Find editorial pages with zero Viator links (supplement to inline_link_audit.py)."""
import sys
import re
from pathlib import Path

SKIP_STEMS = {'about', 'contact', 'privacy', '404', 'index', 'acerca-de', 'contacto', 'privacidad'}

def check(site_path):
    site = Path(site_path)
    editorial = []
    
    for f in sorted(site.rglob('*.html')):
        stem = f.stem.lower()
        if stem in SKIP_STEMS:
            continue
        # Also skip backup dirs
        rel = str(f.relative_to(site))
        if 'backup' in rel.lower():
            continue
        editorial.append(f)
    
    no_viator = []
    for f in editorial:
        content = f.read_text(encoding='utf-8', errors='ignore')
        if not re.search(r'viator\.com', content):
            no_viator.append(str(f.relative_to(site)))
    
    print(f"\nSite: {site.name}")
    print(f"  Live editorial pages: {len(editorial)}")
    print(f"  Pages without Viator links: {len(no_viator)}")
    for p in no_viator:
        print(f"    📄 {p}")

for sp in sys.argv[1:]:
    check(sp)
