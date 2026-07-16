#!/usr/bin/env python3
"""
CTA Contrast Gate — verifies product card CTAs meet WCAG AA (≥4.5:1).
Yogya visual upgrade lesson: shipped with 2.6:1 contrast for 21 pages.

Usage: python3 cta_contrast_gate.py <site_dir>
Exit: 0=pass, 1=fail
"""
import re, sys
from pathlib import Path


def parse_css_color(value):
    """Parse a CSS color value to RGB tuple. Returns None if unparseable."""
    value = value.strip().lower()
    # var(--primary) — resolve from CSS variables
    # For now return a marker; actual resolution happens in check_site()
    if value.startswith('var('):
        return ('var', value)
    # #hex
    if value.startswith('#'):
        h = value.lstrip('#')
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        if len(h) == 6:
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    # rgb(r, g, b) / rgba(r, g, b, a)
    m = re.match(r'rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', value)
    if m:
        return tuple(int(m.group(i)) for i in (1, 2, 3))
    # Named colors
    named = {'white': (255, 255, 255), 'black': (0, 0, 0)}
    if value in named:
        return named[value]
    return None


def relative_luminance(rgb):
    """WCAG 2.0 relative luminance."""
    def channel(c):
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(rgb1, rgb2):
    """WCAG contrast ratio between two RGB tuples."""
    l1 = relative_luminance(rgb1)
    l2 = relative_luminance(rgb2)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def load_css_vars(css_path):
    """Extract CSS custom properties from :root block."""
    vars_map = {}
    try:
        content = css_path.read_text()
    except Exception:
        return vars_map
    # Find :root block
    root_match = re.search(r':root\s*\{([^}]*)\}', content, re.DOTALL)
    if not root_match:
        return vars_map
    for line in root_match.group(1).split('\n'):
        m = re.match(r'\s*--([\w-]+)\s*:\s*(.+?);', line)
        if m:
            vars_map[f'var(--{m.group(1)})'] = m.group(2).strip()
    return vars_map


def check_site(site_dir):
    site = Path(site_dir)
    css = site / 'css' / 'style.css'
    if not css.exists():
        print(f"ERROR: {css} not found")
        return 1

    vars_map = load_css_vars(css)
    
    # Find CTA selectors in CSS
    content = css.read_text()
    failures = []
    
    # Find all CTA button rules
    cta_blocks = re.finditer(
        r'(\.cta\s*\{[^}]*\})|(\.product-card\s+\.cta\s*\{[^}]*\})',
        content, re.DOTALL
    )
    
    for block in cta_blocks:
        css_block = block.group(0)
        bg_match = re.search(r'background\s*:\s*([^;]+);', css_block)
        color_match = re.search(r'color\s*:\s*([^;!]+)', css_block)
        
        if not bg_match or not color_match:
            continue
        
        bg_val = bg_match.group(1).strip()
        fg_val = color_match.group(1).strip()
        
        # Resolve CSS variables
        if bg_val.startswith('var('):
            bg_val = vars_map.get(bg_val, bg_val)
        if fg_val.startswith('var('):
            fg_val = vars_map.get(fg_val, fg_val)
        
        bg_rgb = parse_css_color(bg_val)
        fg_rgb = parse_css_color(fg_val)
        
        if not bg_rgb or not fg_rgb:
            continue
        if bg_rgb[0] == 'var' or fg_rgb[0] == 'var':
            continue
        
        ratio = contrast_ratio(bg_rgb, fg_rgb)
        status = 'PASS' if ratio >= 4.5 else 'FAIL'
        
        if ratio < 4.5:
            failures.append((bg_val, fg_val, ratio))
            print(f"  ❌ CTA: {fg_val} on {bg_val} = {ratio:.1f}:1 (need ≥4.5)")
        else:
            print(f"  ✅ CTA: {fg_val} on {bg_val} = {ratio:.1f}:1")
    
    if failures:
        print(f"\n❌ CTA CONTRAST GATE FAILED — {len(failures)} CTA(s) below 4.5:1")
        return 1
    
    print("\n✅ CTA CONTRAST GATE PASSED")
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: cta_contrast_gate.py <site_dir>")
        sys.exit(1)
    sys.exit(check_site(sys.argv[1]))
