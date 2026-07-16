#!/usr/bin/env python3
"""Pre-generation dedup check — verify no page already exists for a brief.

Uses DB-driven site discovery — no hardcoded site paths.
New sites in site_registry.db are auto-discovered.
"""

import json, os, sys, sqlite3
from pathlib import Path

DB_DIR = Path.home() / ".hermes" / "affiliate-crons" / "db"


def get_site_path(site_slug):
    """Load site local_path from registry. DB-driven — no hardcoded dict."""
    db = DB_DIR / "site_registry.db"
    if not db.exists():
        print(f"ERROR: site_registry.db not found at {db}", file=sys.stderr)
        return None
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT local_path FROM sites WHERE site_id=? AND status='active'",
        (site_slug,)
    ).fetchone()
    conn.close()
    if not row:
        print(f"ERROR: Site '{site_slug}' not found in registry", file=sys.stderr)
        return None
    return row[0]


def check_briefs(site_slug, filtered_path=None):
    if filtered_path is None:
        filtered_path = f"/tmp/filtered-{site_slug}.json"

    if not os.path.exists(filtered_path):
        print(f"No filtered briefs at {filtered_path}")
        return []

    site_dir = get_site_path(site_slug)
    if not site_dir or not os.path.exists(site_dir):
        print(f"Site directory not found: {site_dir}")
        return []

    with open(filtered_path) as f:
        data = json.load(f)

    briefs = data.get("briefs", [])
    duplicates = []

    for brief in briefs:
        slug = brief.get("slug", "")
        if not slug:
            continue

        # Check direct match
        if os.path.exists(os.path.join(site_dir, slug)) or \
           os.path.exists(os.path.join(site_dir, f"{slug}.html")):
            duplicates.append(slug)
            print(f"DUPLICATE: {slug} — page already exists on site")
            continue

        # Check subdirectories
        for root, dirs, files in os.walk(site_dir):
            dirs[:] = [d for d in dirs if d not in ("backup", ".git", "css", "images")]
            if f"{slug}.html" in files:
                duplicates.append(slug)
                print(f"DUPLICATE: {slug} — exists as {os.path.relpath(os.path.join(root, slug), site_dir)}.html")
                break

    if not duplicates:
        print("No duplicates found ✅")
    else:
        print(f"\n{len(duplicates)} duplicate(s) found — these will be skipped by generator")

    return duplicates


if __name__ == "__main__":
    site = sys.argv[1] if len(sys.argv) > 1 else None
    if not site:
        print("Usage: dedup_check.py <site_slug>")
        sys.exit(1)

    dupes = check_briefs(site)
    sys.exit(0)  # Always exit 0 — duplicates are informational, not errors
