#!/usr/bin/env python3
"""
QA Deploy Health — audits deployed pages for sub-threshold QA scores.

Checks qa_state.json for pages with composite score < 7.0 that were deployed.
Catches pages deployed before Phase 4.2 QA gate existed, or deployed through
alternate paths (standalone deploy.py bypassing the orchestrator gate).

Usage: python3 qa_deploy_health.py [--site site_id]

Exit 0: no low-quality deployed pages
Exit 1: pages found with composite < 7.0
"""
import sys, os, json
from datetime import datetime
from pathlib import Path

QA_STATE_FILE = Path.home() / ".hermes" / "affiliate-crons" / "state" / "qa_state.json"
APPROVED_DIR = Path.home() / ".hermes" / "affiliate-crons" / "approved"
QA_THRESHOLD = 7.0


def load_qa_state():
    if QA_STATE_FILE.exists():
        with open(QA_STATE_FILE) as f:
            return json.load(f)
    return {}


def get_deployed_slugs():
    """Get sets of deployed slugs from approved/ directory."""
    deployed = {}
    if APPROVED_DIR.exists():
        for site_dir in APPROVED_DIR.iterdir():
            if not site_dir.is_dir() or site_dir.name.startswith('.'):
                continue
            site_id = site_dir.name
            slugs = set()
            for f in site_dir.iterdir():
                if f.suffix == '.html':
                    slugs.add(f.stem)
            if slugs:
                deployed[site_id] = slugs
    return deployed


def main():
    site_filter = None
    for i, arg in enumerate(sys.argv):
        if arg == "--site" and i + 1 < len(sys.argv):
            site_filter = sys.argv[i + 1]
    
    qa_state = load_qa_state()
    deployed = get_deployed_slugs()
    
    if site_filter and site_filter not in qa_state:
        print(f"ERROR: site '{site_filter}' not found in qa_state.json")
        sys.exit(1)
    
    bad_pages = []
    
    for site_id, site_data in sorted(qa_state.items()):
        if site_filter and site_id != site_filter:
            continue
        if site_id not in deployed:
            continue
        
        reviewed = site_data.get("reviewed", {})
        deployed_slugs = deployed[site_id]
        
        for slug, qa_data in sorted(reviewed.items()):
            if slug not in deployed_slugs:
                continue  # not deployed, skip
            
            composite = qa_data.get("composite", qa_data.get("tier2_score", 0))
            verdict = qa_data.get("verdict", "unknown")
            
            if composite < QA_THRESHOLD:
                bad_pages.append({
                    "site": site_id,
                    "slug": slug,
                    "composite": composite,
                    "tier1": qa_data.get("tier1_score", "?"),
                    "tier2": qa_data.get("tier2_score", "?"),
                    "verdict": verdict,
                    "issues": qa_data.get("tier1_issues", []) + qa_data.get("tier2_issues", [])
                })
    
    if not bad_pages:
        total = sum(len(v) for v in deployed.values())
        print(f"✅ QA deploy health — clean ({total} deployed pages, 0 below threshold {QA_THRESHOLD})")
        sys.exit(0)
    
    total_deployed = sum(len(v) for v in deployed.values())
    print(f"⚠️ QA gaps detected — {len(bad_pages)} deployed page(s) below QA threshold {QA_THRESHOLD}\n")
    
    by_site = {}
    for p in bad_pages:
        by_site.setdefault(p["site"], []).append(p)
    
    for site_id, pages in sorted(by_site.items()):
        print(f"## {site_id} — {len(pages)} page(s)")
        for p in pages:
            issues_preview = "; ".join(p["issues"][:3]) if p["issues"] else "no issues logged"
            print(f"  • {p['slug']} — composite {p['composite']}/10 (T1: {p['tier1']}, T2: {p['tier2']}, {p['verdict']})")
            print(f"    Issues: {issues_preview[:200]}")
    
    print(f"\nThese pages were deployed despite QA scores below {QA_THRESHOLD}.")
    print("Phase 4.2 QA gate now blocks sub-threshold pages from deploying.")
    print("Consider regenerating or reviewing these pages manually.")
    sys.exit(1)


if __name__ == "__main__":
    main()
