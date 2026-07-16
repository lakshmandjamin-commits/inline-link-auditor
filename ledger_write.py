#!/usr/bin/env python3
"""
Build Ledger Writer — sole writer of .build/ledger.json.

Writes phase status, timestamp, and optional artifact SHAs.
Only writes PASS — never FAIL (FAIL is implied by absence).

Usage:
  python3 ledger_write.py <site_dir> <phase> PASS [--artifact <name> <sha256>] [--reviewer opus] [--checks '{"check":"val"}']
"""

import sys
import os
import json
import hashlib
from datetime import datetime, timezone


def sha256_file(path: str) -> str:
    """SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(site_dir: str) -> dict:
    """SHA-256 of every HTML file in the site."""
    shas = {}
    import glob as g
    for html_file in sorted(g.glob(f'{site_dir}/**/*.html', recursive=True)):
        rel = os.path.relpath(html_file, site_dir)
        shas[rel] = sha256_file(html_file)
    return shas


def main():
    if len(sys.argv) < 4:
        print("Usage: ledger_write.py <site_dir> <phase> PASS [--artifact name sha256] [--reviewer name] [--checks json]", file=sys.stderr)
        sys.exit(2)

    site_dir = sys.argv[1]
    phase = sys.argv[2]
    # status is always PASS (called only on success)

    ledger_dir = os.path.join(site_dir, '.build')
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, 'ledger.json')

    # Load existing ledger
    ledger = {}
    if os.path.exists(ledger_path):
        with open(ledger_path) as f:
            ledger = json.load(f)

    # Build phase entry
    entry = {
        'status': 'PASS',
        'ts': datetime.now(timezone.utc).isoformat(),
        'page_shas': sha256_tree(site_dir),
    }

    # Parse optional args
    args = sys.argv[3:]
    i = 0
    while i < len(args):
        if args[i] == '--artifact' and i + 2 < len(args):
            entry.setdefault('artifacts', {})[args[i+1]] = args[i+2]
            i += 3
        elif args[i] == '--reviewer' and i + 1 < len(args):
            entry['reviewer'] = args[i+1]
            i += 2
        elif args[i] == '--checks' and i + 1 < len(args):
            entry['checks'] = json.loads(args[i+1])
            i += 2
        else:
            i += 1

    # Initialize site metadata on first write
    if 'site' not in ledger:
        ledger['site'] = os.path.basename(site_dir.rstrip('/'))
        ledger['started'] = entry['ts']

    ledger.setdefault('phases', {})[phase] = entry

    with open(ledger_path, 'w') as f:
        json.dump(ledger, f, indent=2)

    print(f"ledger: {phase} → PASS ({len(entry.get('page_shas', {}))} files hashed)")


if __name__ == '__main__':
    main()
