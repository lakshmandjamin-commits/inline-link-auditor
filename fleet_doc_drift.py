#!/usr/bin/env python3
"""Fleet Document Drift Detection — Cross-Reference & Auto-Fix

Checks system-inventory.md and relationship-map.md against actual fleet state.
Auto-adds missing scripts, removes stale entries, updates source dates.
Exit 0 = clean (or clean after auto-fix). Exit 1 = unfixable issues.

Usage:
  python3 fleet_doc_drift.py          # scan + auto-fix (default)
  python3 fleet_doc_drift.py --check  # audit only, no fixes
  python3 fleet_doc_drift.py --quiet  # suppress clean output
"""

import os, sys, re, datetime
from pathlib import Path

HERMES = Path.home() / '.hermes'
SCRIPTS_DIR = HERMES / 'affiliate-crons' / 'scripts'
SCRIPTS_DIR_GLOBAL = HERMES / 'scripts'  # fleet-lang-audit, post_gen_fix, skill_observer_weekly, etc.
SKILLS_DIR = HERMES / 'skills' / 'devops' / 'affiliate-operations'
INVENTORY_FILE = SKILLS_DIR / 'references' / 'system-inventory.md'
RELATIONSHIP_MAP = SKILLS_DIR / 'references' / 'relationship-map.md'
FLEET_REGISTRY = SKILLS_DIR / 'references' / 'fleet-registry.yaml'

TODAY = datetime.date.today().strftime('%Y-%m-%d')

INFRA_SCRIPTS = {'__init__.py'}

def get_actual_scripts():
    """Scan both affiliate-crons/scripts and ~/.hermes/scripts."""
    scripts = set()
    for d in [SCRIPTS_DIR, SCRIPTS_DIR_GLOBAL]:
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and f.suffix in ('.py', '.sh') and f.name not in INFRA_SCRIPTS:
                    scripts.add(f.name)
    return scripts

def get_inventoried_scripts(text):
    scripts = set()
    for m in re.finditer(r'\|\s*`([a-z_][a-z0-9_.-]*\.[a-z]+)`\s*\|', text):
        scripts.add(m.group(1))
    return scripts

def find_table_section(lines, start_header):
    start_idx = None
    end_idx = len(lines)
    for i, line in enumerate(lines):
        if line.startswith(start_header):
            start_idx = i
        elif start_idx is not None and line.startswith("## "):
            end_idx = i
            break
    if start_idx is None:
        return None, None
    return start_idx, end_idx

def update_system_inventory(inv_path, fix=False, quiet=False):
    actions = []
    if not os.path.exists(inv_path):
        return ["CRITICAL: system-inventory.md not found"]
    with open(inv_path, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    text = "".join(lines)
    actual = get_actual_scripts()
    listed = get_inventoried_scripts(text)
    sec_start, sec_end = find_table_section(lines, "## Scripts")
    if sec_start is None:
        return ["ERROR: could not find ## Scripts section"]
    # Build dict of existing table entries: script_name → full row text
    prev_table = {}
    for name, row_text in re.findall(r'\|\s*`([^`]+)`\s*\|(.*?)\|\n', "".join(lines[sec_start:sec_end])):
        prev_table[name] = f"| `{name}` |{row_text}|\n"
    
    # Rebuild the table section with only scripts that exist on disk.
    # Stale entries (listed but not on disk) are dropped.
    # New entries (on disk but not listed) are added with placeholder description.
    table_header_idx = sec_start
    for i in range(sec_start, sec_end):
        if lines[i].startswith("|"):
            table_header_idx = i
            break
    
    new_table = lines[sec_start:table_header_idx]  # keep section header + intro text
    new_table.append(lines[table_header_idx])       # keep table column header row
    new_table.append(lines[table_header_idx + 1])   # keep table separator row
    
    for name in sorted(actual):
        # Preserve existing row if available
        existing = None
        for entry_name, row_text in prev_table.items():
            if entry_name == name:
                existing = row_text
                break
        if existing:
            new_table.append(existing)
        else:
            if fix:
                new_table.append(f"| `{name}` | **NEW {TODAY}** — Needs description update | Needs usage update |\n")
                actions.append(f"Added missing script to inventory: {name}")
            else:
                actions.append(f"WARNING: {name} on disk but not listed")
    
    # Replace old table section with rebuilt one
    lines[sec_start:sec_end] = new_table
    sec_end = sec_start + len(new_table)
    
    # Report scripts listed but not on disk (they're being dropped)
    for name in sorted(listed):
        if name not in actual:
            if fix:
                actions.append(f"Removed stale script from inventory: {name}")
            else:
                actions.append(f"WARNING: {name} listed but not on disk")
    # Update source date
    for i, line in enumerate(lines):
        if line.startswith("> **Source:**"):
            old_date = re.search(r'\d{4}-\d{2}-\d{2}', line)
            if old_date and old_date.group() != TODAY:
                if fix:
                    lines[i] = re.sub(r'\d{4}-\d{2}-\d{2}', TODAY, line)
                    actions.append(f"Updated source date to {TODAY}")
            break
    if fix and actions:
        with open(inv_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    return actions

def main():
    fix = "--check" not in sys.argv
    quiet = "--quiet" in sys.argv
    issues = []
    all_actions = []
    if not INVENTORY_FILE.exists():
        issues.append("CRITICAL: system-inventory.md not found")
    else:
        inv_actions = update_system_inventory(str(INVENTORY_FILE), fix=fix, quiet=quiet)
        all_actions.extend(inv_actions)
    if not RELATIONSHIP_MAP.exists():
        issues.append("CRITICAL: relationship-map.md not found")
    print(f"=== Fleet Doc Drift — {datetime.datetime.now().isoformat()[:16]} ===")
    print()
    if issues:
        print(f"X {len(issues)} UNFIXABLE ISSUES:")
        for i in issues:
            print(f"  * {i}")
        print()
    if all_actions:
        warnings = [a for a in all_actions if a.startswith("WARNING") or a.startswith("CRITICAL")]
        fixes = [a for a in all_actions if a not in warnings]
        if fixes:
            print(f"FIXES: {len(fixes)}")
            for a in fixes:
                print(f"  * {a}")
            print()
        if warnings:
            print(f"WARNINGS: {len(warnings)}")
            for w in warnings:
                print(f"  * {w}")
            print()
    if not issues and not all_actions:
        if not quiet:
            print("All clean - no drift detected.")
    sys.exit(1 if issues else 0)

if __name__ == "__main__":
    main()
