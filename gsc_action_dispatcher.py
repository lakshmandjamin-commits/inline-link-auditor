#!/usr/bin/env python3
"""
GSC Action Dispatcher (Dry-Run) — reads verified_actions.json, logs proposed changes to dispatch_log.json.

ALL DRY-RUN. No site modifications. No git. No PRs. No file writes outside dispatch_log.json.
Runs as no_agent=true cron (zero tokens).

Usage: python3 gsc_action_dispatcher.py [--verbose]
"""

import json
import os
import sys
import re
from datetime import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────
VERIFIED_FILE = os.path.expanduser("~/.hermes/data/gsc/verified_actions.json")
DISPATCH_LOG = os.path.expanduser("~/.hermes/data/gsc/dispatch_log.json")
SITES_BASE = os.path.expanduser("~/sites")

# ── Safety: Split Allowlists ──────────────────────────────────────────
PROTECTED_DIRS = {"backup", "node_modules", ".review", "__pycache__"}

# Forbidden strings in any diff
FORBIDDEN = [
    'class="product-card"',
    'href="https://www.viator.com',
    "<nav",
    "<footer",
    '<script type="application/ld+json"',
]

# Max changed lines per remedy type
MAX_DIFF_LINES = {
    "canonical_fix": 5,
    "sitemap_submit": 0,  # API call — no diff
    "meta_rewrite": 10,
}


def validate_target(filepath, remedy):
    """Return (allowed, reason)."""
    p = Path(filepath).resolve()
    sites_base = Path(SITES_BASE).expanduser().resolve()

    # Must be under ~/sites/
    try:
        p.relative_to(sites_base)
    except ValueError:
        return False, f"File {filepath} not under {sites_base}"

    # Must be .html or .xml
    if p.suffix not in (".html", ".xml"):
        return False, f"File {filepath} is not .html or .xml"

    # Not in protected dirs
    parts = set(p.parts)
    if parts & PROTECTED_DIRS:
        return False, f"File {filepath} in protected directory"

    # Canonical/sitemap specific
    if remedy == "canonical_fix" and p.suffix != ".html":
        return False, f"Canonical fix requires .html, got {p.suffix}"
    if remedy == "sitemap_submit" and p.name != "sitemap.xml":
        return False, f"Sitemap submit requires sitemap.xml, got {p.name}"

    return True, "OK"


def generate_canonical_diff(site_dir, target_page, current_canonical, expected_canonical):
    """Generate proposed canonical fix diff. Never writes to disk."""
    page_path = Path(site_dir) / target_page.lstrip("/")
    if not page_path.exists():
        return None, f"Page not found: {page_path}"

    html = page_path.read_text(errors="ignore")

    # Find the canonical tag
    pattern = re.compile(r'<link\s+rel="canonical"\s+href="([^"]*)"\s*/?>', re.IGNORECASE)
    m = pattern.search(html)
    if not m:
        return None, "No canonical tag found"

    old_tag = m.group(0)
    new_tag = old_tag.replace(current_canonical, expected_canonical)

    return {
        "file": str(page_path),
        "old_line": old_tag,
        "new_line": new_tag,
        "lines_changed": 1,
    }, "OK"


def generate_sitemap_action(site_domain):
    """Sitemap submission is an API call — no diff needed."""
    return {
        "file": None,
        "action": "GSC_API_SUBMIT",
        "method": "POST webmasters/v3/sites/sc-domain:{}/sitemaps/submit".format(site_domain.replace(".", "%2E")),
        "lines_changed": 0,
    }, "OK"


def validate_diff(diff, remedy):
    """Check proposed diff against safety rules."""
    if not diff:
        return True, "OK"

    for forbidden in FORBIDDEN:
        if "new_line" in diff and forbidden.lower() in diff["new_line"].lower():
            return False, f"Diff contains forbidden content: {forbidden[:50]}"
        if "old_line" in diff and forbidden.lower() in diff["old_line"].lower():
            # old_line containing forbidden is expected (we're fixing it)
            # but new_line should NOT contain it
            pass

    max_lines = MAX_DIFF_LINES.get(remedy, 100)
    if diff.get("lines_changed", 0) > max_lines:
        return False, f"Diff exceeds max {max_lines} lines (got {diff['lines_changed']})"

    return True, "OK"


def main():
    verbose = "--verbose" in sys.argv

    if not os.path.exists(VERIFIED_FILE):
        print(f"No verified file at {VERIFIED_FILE} — nothing to dispatch.")
        sys.exit(0)

    with open(VERIFIED_FILE) as f:
        verified = json.load(f)

    actions = verified.get("actions", [])
    dispatched = []
    blocked = []

    # Load site registry for domain mapping
    import sqlite3
    reg = sqlite3.connect(os.path.expanduser("~/.hermes/affiliate-crons/db/site_registry.db"))
    sites = {row[0]: row[1] for row in reg.execute("SELECT site_id, local_path FROM sites WHERE status='active'")}
    domains = {row[0]: row[1] for row in reg.execute("SELECT site_id, domain FROM sites WHERE status='active'")}
    reg.close()

    for action in actions:
        if action["dispatch_class"] not in ("AUTO_FIX", "PIPELINE"):
            continue  # MONITOR and ESCALATE — nothing to dispatch

        fingerprint = action["action_fingerprint"]
        site_id = action["site_id"]
        target_page = action.get("target_page", "")
        proposed_remedy = action.get("proposed_remedy", "")
        site_dir = sites.get(site_id)
        domain = domains.get(site_id, "")

        if not site_dir:
            blocked.append({"fingerprint": fingerprint, "reason": f"Site {site_id} not found in registry"})
            continue

        # ── Validate target ──
        if proposed_remedy != "sitemap_submit":
            full_path = os.path.join(site_dir, target_page.lstrip("/"))
            allowed, reason = validate_target(full_path, proposed_remedy)
            if not allowed:
                blocked.append({"fingerprint": fingerprint, "reason": f"BLOCKED: {reason}"})
                continue

        # ── Generate diff ──
        if proposed_remedy == "canonical_fix":
            ce = action.get("canonical_evidence", action.get("evidence", {}))
            current = ce.get("current_canonical", "")
            expected = ce.get("expected_canonical", "")
            diff, reason = generate_canonical_diff(site_dir, target_page, current, expected)
        elif proposed_remedy == "sitemap_submit":
            diff, reason = generate_sitemap_action(domain)
        else:
            diff, reason = {"lines_changed": 0}, f"Dry-run only — {proposed_remedy} not implemented yet"

        # ── Validate diff ──
        if diff:
            diff_ok, diff_reason = validate_diff(diff, proposed_remedy)
            if not diff_ok:
                blocked.append({"fingerprint": fingerprint, "reason": f"DIFF BLOCKED: {diff_reason}"})
                continue

        # ── Log ──
        dispatched.append({
            "action_fingerprint": fingerprint,
            "site_id": site_id,
            "target_page": target_page,
            "proposed_remedy": proposed_remedy,
            "dispatch_class": action["dispatch_class"],
            "timestamp": datetime.now().isoformat(),
            "dry_run": True,
            "diff": diff,
            "diff_valid": True if diff else None,
            "reason": reason,
        })

        if verbose and diff:
            print(f"DISPATCH: {site_id} | {proposed_remedy} | {target_page}")
            if diff.get("old_line"):
                print(f"  - {diff['old_line'][:100]}")
                print(f"  + {diff['new_line'][:100]}")

    # ── Write dispatch log ──
    log = {
        "dispatched_at": datetime.now().isoformat(),
        "source_verified": verified.get("verified_at", "unknown"),
        "dry_run": True,
        "summary": {
            "dispatched": len(dispatched),
            "blocked": len(blocked),
            "total_auto_fix": sum(1 for a in actions if a["dispatch_class"] == "AUTO_FIX"),
            "total_pipeline": sum(1 for a in actions if a["dispatch_class"] == "PIPELINE"),
        },
        "dispatched": dispatched,
        "blocked": blocked,
    }

    os.makedirs(os.path.dirname(DISPATCH_LOG), exist_ok=True)
    with open(DISPATCH_LOG, "w") as f:
        json.dump(log, f, indent=2)

    print(f"Dispatch log: {len(dispatched)} dispatched, {len(blocked)} blocked, {len(dispatched)+len(blocked)} total")
    sys.exit(0)


if __name__ == "__main__":
    main()
