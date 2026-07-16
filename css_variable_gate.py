#!/usr/bin/env python3
"""
Undefined CSS Variable Gate — verifies every var(--...) reference is defined in :root.
Yogya visual upgrade lesson: 4 variables were referenced but never defined.

Usage: python3 css_variable_gate.py <site_dir>
Exit: 0=pass, 1=fail
"""
import re, sys
from pathlib import Path


def extract_defined_vars(css_content):
    """Extract all --variable: value definitions from CSS."""
    defined = set()
    # Find :root or host block — handle multiline
    root_blocks = re.finditer(r':root\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}', css_content, re.DOTALL)
    for root_match in root_blocks:
        for m in re.finditer(r'--([\w-]+)\s*:', root_match.group(1)):
            defined.add(f'--{m.group(1)}')
    # Also catch loose --var: value outside blocks
    for m in re.finditer(r'(?<!var\()--([\w-]+)\s*:', css_content):
        if not any(f'var(--{m.group(1)})' in css_content[:m.start()] for _ in [1]):
            defined.add(f'--{m.group(1)}')
    return defined


def extract_referenced_vars(css_content):
    """Extract all var(--...) references, stripping fallback values."""
    referenced = set()
    for m in re.finditer(r'var\((--[\w-]+)', css_content):
        referenced.add(m.group(1))
    return referenced


def check_site(site_dir):
    site = Path(site_dir)
    css = site / 'css' / 'style.css'
    if not css.exists():
        print(f"ERROR: {css} not found")
        return 1

    content = css.read_text()
    defined = extract_defined_vars(content)
    referenced = extract_referenced_vars(content)
    
    undefined = referenced - defined
    
    if undefined:
        print(f"❌ UNDEFINED CSS VARIABLES ({len(undefined)}):")
        for var in sorted(undefined):
            # Find where referenced
            for line_num, line in enumerate(content.split('\n'), 1):
                if f'var({var})' in line:
                    print(f"  {var} — referenced line {line_num}: {line.strip()[:80]}")
                    break
        print(f"\n❌ CSS VARIABLE GATE FAILED — {len(undefined)} undefined variable(s)")
        return 1
    
    print(f"✅ CSS VARIABLE GATE PASSED — {len(referenced)} var() references, {len(defined)} defined in :root")
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: css_variable_gate.py <site_dir>")
        sys.exit(1)
    sys.exit(check_site(sys.argv[1]))
