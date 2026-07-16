#!/usr/bin/env python3
"""
CSS Class Inventory Gate — verifies all HTML class="..." tokens have CSS definitions.
Yogya visual upgrade lesson: 13+ classes used in HTML had zero CSS rules.

Usage: python3 css_class_inventory_gate.py <site_dir>
Exit: 0=pass (all classes defined), 1=fail (undefined classes found)
"""
import re, sys
from pathlib import Path


def extract_html_classes(html_content):
    """Extract all unique class values from HTML class attributes."""
    classes = set()
    for m in re.finditer(r'class="([^"]+)"', html_content):
        for cls in m.group(1).split():
            # Skip utility/pseudo classes
            if cls in ('open', 'active', 'no-scroll'):
                continue
            classes.add(cls)
    return classes


def extract_css_selectors(css_content):
    """Extract all unique class selectors from CSS."""
    selectors = set()
    # Match .class-name patterns in CSS selectors
    for m in re.finditer(r'\.([a-zA-Z_-][\w-]*)', css_content):
        selector = m.group(1)
        # Skip pseudo-classes and HTML elements
        if selector in ('html', 'body', 'button', 'after', 'before', 'hover', 
                        'focus', 'focus-visible', 'nth-child', 'open', 'active',
                        'not', 'first-child', 'last-child', 'nth-of-type',
                        'only-child', 'empty', 'root', 'checked', 'disabled',
                        'enabled', 'required', 'optional', 'read-only', 'read-write',
                        'valid', 'invalid', 'in-range', 'out-of-range', 'target',
                        'lang', 'dir', 'link', 'visited', 'first-letter', 'first-line',
                        'selection', 'placeholder', 'marker', 'backdrop', 'fullscreen'):
            continue
        selectors.add(selector)
    return selectors


def check_site(site_dir):
    site = Path(site_dir)
    css = site / 'css' / 'style.css'
    if not css.exists():
        print(f"ERROR: {css} not found")
        return 1

    css_content = css.read_text()
    css_selectors = extract_css_selectors(css_content)
    
    # Collect all HTML classes
    all_html_classes = set()
    for html_file in site.rglob('*.html'):
        if '.review' in str(html_file):
            continue
        try:
            html_content = html_file.read_text()
            all_html_classes.update(extract_html_classes(html_content))
        except Exception:
            continue
    
    # Find undefined
    undefined = all_html_classes - css_selectors
    # Classes that don't need CSS (browser handles natively) or are styled as HTML elements
    skip = {'skip-link', 'skipnav', 'hamburger'}
    # Check if CSS has element-level selectors for these tags
    for tag in ['nav', 'footer', 'header', 'main', 'section', 'article', 'body', 'html']:
        if re.search(rf'^{tag}\s*\{{\s*', css_content, re.MULTILINE) or re.search(rf'\b{tag}\s*\{{', css_content):
            skip.add(tag)
    undefined = undefined - skip
    
    if undefined:
        print(f"❌ UNDEFINED CSS CLASSES ({len(undefined)}):")
        for cls in sorted(undefined):
            # Find which files use it
            files = []
            for html_file in site.rglob('*.html'):
                if '.review' in str(html_file):
                    continue
                try:
                    if re.search(rf'class="[^"]*\b{cls}\b', html_file.read_text()):
                        files.append(str(html_file.relative_to(site)))
                except Exception:
                    pass
            print(f"  .{cls} — used in {len(files)} file(s): {', '.join(files[:3])}")
        print(f"\n❌ CSS CLASS INVENTORY GATE FAILED — {len(undefined)} undefined class(es)")
        return 1
    
    print(f"✅ CSS CLASS INVENTORY GATE PASSED — {len(all_html_classes)} classes in HTML, all defined in CSS")
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: css_class_inventory_gate.py <site_dir>")
        sys.exit(1)
    sys.exit(check_site(sys.argv[1]))
