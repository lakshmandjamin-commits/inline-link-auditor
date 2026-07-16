#!/usr/bin/env python3
"""
GSC Action Verifier — reads action_items.json, applies policy matrix, outputs verified_actions.json.

All read-only. No site modifications. Exit 0 always (detection only).
Runs as no_agent=true cron (zero tokens).

Usage: python3 gsc_action_verifier.py [--verbose]
"""

import json
import os
import sys
from datetime import datetime, timedelta

# ── Policy Matrix ─────────────────────────────────────────────────────
POLICY = [
    # (min_age, max_age, min_clicks, auto_fix, content_allowed, max_actions, tier_name)
    (0,   21,  0,   ["sitemap_submit"],                [],  1, "Seed"),
    (22,  35,  0,   ["sitemap_submit", "canonical_fix"], [], 2, "Emerging"),
    (22,  35,  10,  ["sitemap_submit", "canonical_fix"], [], 3, "Emerging+"),
    (36,  999, 0,   ["sitemap_submit", "canonical_fix"], [], 2, "Established"),
    (36,  999, 5,   ["sitemap_submit", "canonical_fix"], ["content_expansion"], 4, "Established+"),
]

# ── Paths ─────────────────────────────────────────────────────────────
ACTION_FILE = os.path.expanduser("~/.hermes/data/gsc/action_items.json")
STATE_FILE = os.path.expanduser("~/.hermes/data/gsc/action_state.json")
VERIFIED_FILE = os.path.expanduser("~/.hermes/data/gsc/verified_actions.json")

# ── State Management ──────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Fingerprint ───────────────────────────────────────────────────────
def generate_fingerprint(site_id, issue_type, target_page, target_query, proposed_remedy):
    tq = (target_query or "none").lower()
    return f"{site_id}::{issue_type}::{target_page}::{tq}::{proposed_remedy}".lower()

# ── Tier Resolution ───────────────────────────────────────────────────
def resolve_tier(site_age_days, site_clicks_7d):
    for min_age, max_age, min_clicks, auto_fix, content, max_act, name in POLICY:
        if min_age <= site_age_days <= max_age and site_clicks_7d >= min_clicks:
            return {
                "tier_name": name,
                "auto_fix_allowed": auto_fix,
                "content_allowed": content,
                "max_actions_per_week": max_act,
            }
    return {"tier_name": "Unknown", "auto_fix_allowed": [], "content_allowed": [], "max_actions_per_week": 0}

# ── CTR Emergency Check ───────────────────────────────────────────────
def check_ctr_emergency(state, fingerprint, action):
    """Two-report confirmation for CTR emergencies."""
    ev = action.get("evidence", {})
    cm = ev.get("current_metrics", {})
    impressions = cm.get("impressions", 0)
    ctr = cm.get("ctr", 0)
    position = cm.get("position", 99)

    if impressions > 50 and ctr == 0 and position < 20:
        existing = state.get(fingerprint, {})
        consecutive = existing.get("consecutive_weeks", 0) + 1
        return consecutive >= 2, consecutive
    return False, 0

# ── Main ──────────────────────────────────────────────────────────────
def main():
    verbose = "--verbose" in sys.argv

    if not os.path.exists(ACTION_FILE):
        print(f"No action file at {ACTION_FILE} — nothing to verify.")
        sys.exit(0)

    with open(ACTION_FILE) as f:
        report = json.load(f)

    actions = report.get("actions", [])
    if not actions:
        print("No actions in report — nothing to verify.")
        sys.exit(0)

    state = load_state()
    verified = []
    today = datetime.now().date()

    for action in actions:
        # ── Extract fields ──
        site_id = action.get("site_id", "")
        issue_type = action.get("issue_type", "")
        target_page = action.get("target_page", "")
        target_query = action.get("target_query")
        proposed_remedy = action.get("proposed_remedy", "monitor_only")
        supplied_fingerprint = action.get("action_fingerprint", "")

        # ── Validate fingerprint ──
        expected_fp = generate_fingerprint(site_id, issue_type, target_page, target_query, proposed_remedy)
        fingerprint_ok = supplied_fingerprint == expected_fp

        if not fingerprint_ok and verbose:
            print(f"FINGERPRINT MISMATCH: {site_id} — supplied={supplied_fingerprint} expected={expected_fp}")

        # Use expected fingerprint (programmatic — never trust supplied)
        fingerprint = expected_fp

        # ── Resolve tier ──
        evidence = action.get("evidence", {})
        site_age = evidence.get("site_age_days", 999)
        site_clicks = evidence.get("site_clicks_7d", 0)
        tier = resolve_tier(site_age, site_clicks)

        # ── Determine dispatch_class ──
        dispatch_class = "MONITOR"  # default
        reason = ""
        confidence = 0.0

        # Check CTR emergency
        is_ctr_emergency, ctr_weeks = check_ctr_emergency(state, fingerprint, action)

        if proposed_remedy in tier["auto_fix_allowed"]:
            if proposed_remedy == "canonical_fix":
                # Requires explicit canonical evidence
                ce = action.get("canonical_evidence")
                if ce and all(k in ce for k in ["current_canonical", "expected_canonical", "http_status_current", "sitemap_loc"]):
                    dispatch_class = "AUTO_FIX"
                    confidence = 0.9
                    reason = f"Canonical evidence complete — {ce['current_canonical']} → {ce['expected_canonical']}"
                else:
                    dispatch_class = "MONITOR"
                    confidence = 0.3
                    reason = "Insufficient canonical evidence — need current/expected/http_status/sitemap_loc"
            elif proposed_remedy == "sitemap_submit":
                dispatch_class = "AUTO_FIX"
                confidence = 0.95
                reason = "Sitemap submission — admin API call, zero risk"
            else:
                dispatch_class = "AUTO_FIX"
                confidence = 0.8
                reason = f"Allowed by policy tier {tier['tier_name']}"

        elif proposed_remedy in tier["content_allowed"]:
            if is_ctr_emergency:
                dispatch_class = "PIPELINE"
                confidence = 0.7
                reason = f"CTR emergency confirmed across {ctr_weeks} consecutive reports"
            else:
                dispatch_class = "PIPELINE"
                confidence = 0.5
                reason = f"Content expansion allowed for tier {tier['tier_name']} — staged for review"

        elif site_age < 21:
            dispatch_class = "MONITOR"
            confidence = 0.1
            reason = f"Site age {site_age}d < 21 — too young, monitor only"

        else:
            dispatch_class = "MONITOR"
            confidence = 0.2
            reason = f"Remedy '{proposed_remedy}' not allowed for tier {tier['tier_name']} (age={site_age}d, clicks={site_clicks})"

        # ── Update state ──
        existing = state.get(fingerprint, {})
        is_new = not existing

        state[fingerprint] = {
            "uuid": existing.get("uuid", f"{fingerprint}-{today.isoformat()}"),
            "lifecycle": "verified",
            "first_seen": existing.get("first_seen", today.isoformat()),
            "last_seen": today.isoformat(),
            "consecutive_weeks": existing.get("consecutive_weeks", 0) + 1,
            "retry_count": existing.get("retry_count", 0),
            "deploy_id": None,
            "commit_sha": None,
        }

        # ── Build verified entry ──
        verified.append({
            "action_fingerprint": fingerprint,
            "fingerprint_valid": fingerprint_ok,
            "site_id": site_id,
            "issue_type": issue_type,
            "target_page": target_page,
            "target_query": target_query,
            "proposed_remedy": proposed_remedy,
            "dispatch_class": dispatch_class,
            "confidence": round(confidence, 2),
            "risk_level": action.get("risk_level", "low"),
            "policy_tier": tier["tier_name"],
            "site_age_days": site_age,
            "site_clicks_7d": site_clicks,
            "consecutive_weeks": state[fingerprint]["consecutive_weeks"],
            "reason": reason,
            "first_seen": is_new,
        })

    # ── Save state + verified output ──
    save_state(state)

    os.makedirs(os.path.dirname(VERIFIED_FILE), exist_ok=True)
    with open(VERIFIED_FILE, "w") as f:
        json.dump({
            "verified_at": datetime.now().isoformat(),
            "source_report": report.get("generated_at", "unknown"),
            "actions": verified,
            "summary": {
                "total": len(verified),
                "auto_fix": sum(1 for v in verified if v["dispatch_class"] == "AUTO_FIX"),
                "pipeline": sum(1 for v in verified if v["dispatch_class"] == "PIPELINE"),
                "monitor": sum(1 for v in verified if v["dispatch_class"] == "MONITOR"),
                "fingerprint_mismatches": sum(1 for v in verified if not v["fingerprint_valid"]),
            }
        }, f, indent=2)

    summary = f"Verified {len(verified)} actions: "
    summary += f"{sum(1 for v in verified if v['dispatch_class']=='AUTO_FIX')} AUTO_FIX, "
    summary += f"{sum(1 for v in verified if v['dispatch_class']=='PIPELINE')} PIPELINE, "
    summary += f"{sum(1 for v in verified if v['dispatch_class']=='MONITOR')} MONITOR"
    print(summary)
    if verbose:
        for v in verified:
            print(f"  {v['dispatch_class']:>10} | {v['site_id']} | {v['issue_type']} | {v['reason'][:80]}")

    sys.exit(0)


if __name__ == "__main__":
    main()
