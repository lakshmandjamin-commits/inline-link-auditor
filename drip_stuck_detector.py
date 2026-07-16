#!/usr/bin/env python3
"""
Drip Stuck Detector — checks generated/ for pages not advancing to QA/approved.

Usage: python3 drip_stuck_detector.py [--clean] [--site site_id]
  --clean: delete stuck pages older than 48h, log to state file
  --site: check a specific site only

Exit 0: no stuck pages (clean)
Exit 1: stuck pages found (alert)
"""
import sys, os, json, time
from datetime import datetime, timedelta
from pathlib import Path

GENERATED_DIR = Path.home() / ".hermes" / "affiliate-crons" / "generated"
APPROVED_DIR = Path.home() / ".hermes" / "affiliate-crons" / "approved"
GEN_STATE_FILE = Path.home() / ".hermes" / "affiliate-crons" / "state" / "generation_state.json"
QA_STATE_FILE = Path.home() / ".hermes" / "affiliate-crons" / "state" / "qa_state.json"
STUCK_THRESHOLD_HOURS = 24
CLEAN_THRESHOLD_HOURS = 48


def load_json(path):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def get_stuck_pages(site_id, gen_state, qa_state):
    """Return stuck pages: in generated/ but not in approved/, older than threshold."""
    gen_dir = GENERATED_DIR / site_id
    approved_dir = APPROVED_DIR / site_id
    
    if not gen_dir.exists():
        return []
    
    # Get slugs of approved pages
    approved_slugs = set()
    if approved_dir.exists():
        for f in approved_dir.iterdir():
            if f.suffix == '.html':
                approved_slugs.add(f.stem)
    
    # Get slugs from generation_state (pages the pipeline generated)
    gen_slugs = set(gen_state.get(site_id, {}).get("generated", []))
    # Get QA-reviewed slugs
    qa_slugs = set()
    site_qa = qa_state.get(site_id, {}).get("reviewed", {})
    qa_slugs = set(site_qa.keys())
    
    now = datetime.now()
    stuck = []
    
    for f in gen_dir.iterdir():
        if not f.suffix == '.html':
            continue
        slug = f.stem
        # Skip if already approved
        if slug in approved_slugs:
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        age_hours = (now - mtime).total_seconds() / 3600
        
        if age_hours < STUCK_THRESHOLD_HOURS:
            continue  # too fresh
        
        # Classify why it's stuck
        if slug in gen_slugs and slug not in qa_slugs:
            reason = "not QA-reviewed"
        elif slug in qa_slugs:
            site_qa_data = qa_state.get(site_id, {}).get("reviewed", {}).get(slug, {})
            verdict = site_qa_data.get("verdict", "unknown")
            if verdict in ("REGENERATE", "DISCARD"):
                reason = f"QA {verdict}"
            else:
                reason = "QA passed but not deployed"
        else:
            reason = "not in generation state (orphan)"
        
        stuck.append((slug, round(age_hours, 1), str(f), reason))
    
    return sorted(stuck, key=lambda x: -x[1])  # oldest first


def main():
    clean_mode = "--clean" in sys.argv
    site_filter = None
    for i, arg in enumerate(sys.argv):
        if arg == "--site" and i + 1 < len(sys.argv):
            site_filter = sys.argv[i + 1]
    
    gen_state = load_json(GEN_STATE_FILE)
    qa_state = load_json(QA_STATE_FILE)
    
    sites = [d.name for d in GENERATED_DIR.iterdir() if d.is_dir() and not d.name.startswith('.')]
    if site_filter:
        if site_filter not in sites:
            print(f"ERROR: site '{site_filter}' not found in generated/")
            sys.exit(1)
        sites = [site_filter]
    
    all_stuck = {}
    for site_id in sorted(sites):
        stuck = get_stuck_pages(site_id, gen_state, qa_state)
        if stuck:
            all_stuck[site_id] = stuck
    
    if not all_stuck:
        print(f"✅ Drip health — clean ({len(sites)} sites, 0 stuck)")
        sys.exit(0)
    
    total = sum(len(v) for v in all_stuck.values())
    print(f"⚠️ Stuck pages detected — {total} across {len(all_stuck)} site(s)\n")
    
    for site_id, pages in sorted(all_stuck.items()):
        print(f"## {site_id} — {len(pages)} stuck")
        for slug, age_h, path, reason in pages:
            print(f"  • {slug} — {age_h}h ({reason})")
    
    if clean_mode:
        print(f"\n---\n🔧 Auto-clean: removing pages older than {CLEAN_THRESHOLD_HOURS}h...")
        cleaned = 0
        for site_id, pages in all_stuck.items():
            for slug, age_h, path, reason in pages:
                if age_h >= CLEAN_THRESHOLD_HOURS:
                    try:
                        os.remove(path)
                        cleaned += 1
                        print(f"  🗑️ {site_id}/{slug} — deleted ({age_h}h, {reason})")
                    except OSError as e:
                        print(f"  ❌ {site_id}/{slug} — delete failed: {e}")
        if cleaned:
            print(f"\n✅ Cleaned {cleaned} stuck pages")
        else:
            print(f"\nNo pages older than {CLEAN_THRESHOLD_HOURS}h to clean.")
    
    print(f"\nRun with --clean to auto-delete stuck pages older than {CLEAN_THRESHOLD_HOURS}h.")
    sys.exit(1)


if __name__ == "__main__":
    main()
