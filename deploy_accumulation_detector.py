#!/usr/bin/env python3
"""
Deploy Accumulation Detector — checks approved/ for pages not deployed within 24h.

Usage: python3 deploy_accumulation_detector.py [--fix] [--site site_id]
  --fix: attempt deploy.py for pages older than 24h
  --site: check a specific site only

Exit 0: no backlog (clean)
Exit 1: backlog found (alert)
Exit 2: --fix ran but deployment failed
"""
import sys, os, json, subprocess, time
from datetime import datetime, timedelta
from pathlib import Path

APPROVED_DIR = Path.home() / ".hermes" / "affiliate-crons" / "approved"
STATE_FILE = Path.home() / ".hermes" / "affiliate-crons" / "state" / "deploy_state.json"
SCRIPTS_DIR = Path.home() / ".hermes" / "affiliate-crons" / "scripts"
STALE_HOURS = 24


def load_deploy_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def get_old_files(site_id, state, cutoff):
    """Return list of (filename, age_hours) for approved files older than cutoff."""
    site_dir = APPROVED_DIR / site_id
    if not site_dir.exists():
        return []
    
    deployed_slugs = set(state.get(site_id, {}).get("deployed", []))
    old_files = []
    
    for f in sorted(site_dir.iterdir()):
        if not f.suffix == '.html':
            continue
        slug = f.stem
        if slug in deployed_slugs:
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        age_hours = (datetime.now() - mtime).total_seconds() / 3600
        if age_hours > STALE_HOURS:
            old_files.append((slug, round(age_hours, 1), str(f)))
    
    return old_files


def main():
    fix_mode = "--fix" in sys.argv
    site_filter = None
    for i, arg in enumerate(sys.argv):
        if arg == "--site" and i + 1 < len(sys.argv):
            site_filter = sys.argv[i + 1]
    
    state = load_deploy_state()
    
    # Get all site dirs
    sites = [d.name for d in APPROVED_DIR.iterdir() if d.is_dir() and not d.name.startswith('.')]
    if site_filter:
        if site_filter not in sites:
            print(f"ERROR: site '{site_filter}' not found in approved/")
            sys.exit(1)
        sites = [site_filter]
    
    now = datetime.now()
    cutoff = now - timedelta(hours=STALE_HOURS)
    
    all_backlog = {}
    for site_id in sorted(sites):
        old = get_old_files(site_id, state, cutoff)
        if old:
            all_backlog[site_id] = old
    
    if not all_backlog:
        print(f"✅ Deploy health — clean ({len(sites)} sites, 0 backlog)")
        sys.exit(0)
    
    # Report backlog
    total = sum(len(v) for v in all_backlog.values())
    print(f"⚠️ Deploy accumulation detected — {total} pages across {len(all_backlog)} site(s)\n")
    
    for site_id, files in sorted(all_backlog.items()):
        print(f"## {site_id} — {len(files)} page(s)")
        for slug, age_h, path in files:
            print(f"  • {slug} — {age_h}h old")
    
    # Auto-heal if --fix
    if fix_mode:
        print(f"\n---\n🔧 Auto-heal: attempting deploy for {total} page(s)...")
        failures = 0
        for site_id in sorted(all_backlog.keys()):
            deploy_script = SCRIPTS_DIR / "deploy.py"
            if not deploy_script.exists():
                print(f"  ❌ deploy.py not found at {deploy_script}")
                sys.exit(2)
            result = subprocess.run(
                [sys.executable, str(deploy_script), site_id],
                capture_output=True, text=True, timeout=120,
                cwd=str(SCRIPTS_DIR)
            )
            if result.returncode == 0:
                print(f"  ✅ {site_id} — deployed")
            else:
                print(f"  ❌ {site_id} — deploy failed (exit {result.returncode})")
                print(f"     {result.stderr.strip()[:200]}")
                failures += 1
        
        if failures:
            print(f"\n❌ {failures} site(s) failed to deploy")
            sys.exit(2)
        else:
            print(f"\n✅ All {total} pages deployed")
            sys.exit(0)
    
    print(f"\nRun with --fix to auto-deploy accumulated pages.")
    sys.exit(1)


if __name__ == "__main__":
    main()
