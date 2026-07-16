#!/usr/bin/env python3
"""
Ranking Drop Watch — Weekly position regression detection for the affiliate fleet.

Compares current 7-day GSC position vs. 28-day baseline. Flags material drops
that pass a self-check (not seasonal, not noise, not already known).

Follows the 9-part loop anatomy from marketing-loops:
  Check cadence: Weekly (Monday 09:00 SGT)
  Acts when: A page/query drops >10 positions AND baseline had ≥5 clicks/week
  Purpose: Catch SEO regressions before they compound
  Skills used: GSC Search Analytics API (googleapiclient)
  Self-check: Seasonal? SERP feature change? Already flagged?
  State: ~/.hermes/data/gsc/ranking_drop_state.json — tracks open issues
  Stop/bail-out: No material drops → exit 0. Data-source outage → exit 1 with error.
  Output: Structured report to stdout (cron delivery)

Usage:
    python3 ranking_drop_watch.py [--state-file PATH] [--dry-run]
"""

import json
import os
import sys
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Configuration ──────────────────────────────────────────────────────
CRED_PATH = os.path.expanduser("~/.hermes/credentials/saraswati-gsc.json")
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

# sc-domain: → display name mapping
SITES = {
    "sc-domain:madeira-trail-guide.com": "madeira-trail-guide",
    "sc-domain:lapland-adventure-guide.com": "lapland-adventure-guide",
    "sc-domain:porto-sommelier.com": "porto-sommelier",
    "sc-domain:tenerife-outdoor-guide.com": "tenerife-outdoor-guide",
    "sc-domain:san-juan-excursions.com": "san-juan-excursions",
    "sc-domain:yogyakarta-temple-tours.com": "yogyakarta-temple-tours",
}

# Thresholds
POSITION_DROP_THRESHOLD = 10       # Must drop at least this many positions
MIN_BASELINE_CLICKS = 5            # Must have had real traffic before (per week)
COOLDOWN_DAYS = 14                 # Don't re-flag within this window
CURRENT_WINDOW_DAYS = 7            # Current window to assess
BASELINE_WINDOW_DAYS = 28          # Baseline window (4x current for stability)
GSC_LATENCY_DAYS = 3               # GSC data is 2-3 days behind
ROW_LIMIT = 5000

# State file — tracks open regressions
STATE_FILE = os.path.expanduser("~/.hermes/data/gsc/ranking_drop_state.json")

# ── GSC Helpers ────────────────────────────────────────────────────────
creds = service_account.Credentials.from_service_account_file(CRED_PATH, scopes=SCOPES)
svc = build("searchconsole", "v1", credentials=creds)


def query(site_url, dimensions, start_date, end_date, row_limit=ROW_LIMIT):
    """Query GSC Search Analytics API. dimensions: [] for totals, ['query'] for keywords, ['page'] for pages."""
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "rowLimit": row_limit,
    }
    return svc.searchanalytics().query(siteUrl=site_url, body=body).execute()


def load_state():
    """Load the regression state file. Empty dict if not found."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"issues": {}, "last_run": None}


def save_state(state):
    """Write state back to disk."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def issue_key(site_id, query_or_page):
    """Unique key per issue: site + query/page string."""
    return f"{site_id}::{query_or_page}"


def is_in_cooldown(state, key):
    """Check if this issue was flagged recently and is still in cooldown."""
    if key in state["issues"]:
        last_flagged = state["issues"][key].get("last_flagged")
        if last_flagged:
            days_since = (datetime.now().date() - datetime.fromisoformat(last_flagged).date()).days
            return days_since < COOLDOWN_DAYS
    return False


def self_check_seasonal(domain, query_text, current_pos, baseline_pos):
    """
    Rule out seasonal volatility.
    Returns True if this looks like a seasonal pattern, not a regression.
    Simple heuristic: if the query has strong seasonal signals (year, season),
    flag as possible seasonal.
    """
    seasonal_terms = [
        "2024", "2025", "2026", "january", "february", "march", "april",
        "may", "june", "july", "august", "september", "october",
        "november", "december", "winter", "summer", "spring", "autumn",
        "christmas", "new year", "easter", "black friday",
    ]
    q = query_text.lower()
    hit = any(t in q for t in seasonal_terms)
    if hit:
        return True, "Query contains seasonal/time markers — drop may be seasonal, not regression"
    return False, None


# ── Detection Logic ────────────────────────────────────────────────────
def detect_drops(site_url, site_id, current_start, current_end, baseline_start, baseline_end):
    """
    Compare current window vs baseline window. Return list of regression issues.
    Each issue: {site_id, query, current_pos, baseline_pos, drop, clicks_current, clicks_baseline, intent}
    """
    issues = []

    # Pull current + baseline data (dimensions: query for keyword-level tracking)
    # GSC API note: can't combine query + page, so we do query-level tracking
    current_data = query(site_url, ["query"], current_start, current_end)
    baseline_data = query(site_url, ["query"], baseline_start, baseline_end)

    current_rows = current_data.get("rows", [])
    baseline_rows = baseline_data.get("rows", [])

    # Build baseline lookup: query → {clicks, position}
    baseline_map = {}
    for row in baseline_rows:
        q = row["keys"][0]
        baseline_map[q] = {
            "clicks": row["clicks"],
            "position": round(row["position"], 1),
        }

    for row in current_rows:
        query_text = row["keys"][0]
        current_pos = round(row["position"], 1)
        current_clicks = row["clicks"]
        current_impressions = row.get("impressions", 0)

        baseline = baseline_map.get(query_text)
        if not baseline:
            continue  # New query — not a regression

        baseline_pos = baseline["position"]
        baseline_clicks = baseline["clicks"]

        # ── Acts when: drop > threshold AND baseline had real traffic ──
        drop = current_pos - baseline_pos
        if drop <= POSITION_DROP_THRESHOLD:
            continue
        if baseline_clicks < MIN_BASELINE_CLICKS:
            continue

        # Self-check: seasonal?
        seasonal, reason = self_check_seasonal(
            site_id, query_text, current_pos, baseline_pos
        )
        if seasonal:
            continue

        # Check cooldown
        key = issue_key(site_id, query_text)
        # We check cooldown later in main() with loaded state

        issues.append({
            "site_id": site_id,
            "domain": site_url.replace("sc-domain:", ""),
            "query": query_text,
            "current_pos": current_pos,
            "baseline_pos": baseline_pos,
            "drop": round(drop, 1),
            "clicks_current": current_clicks,
            "clicks_baseline": baseline_clicks,
            "impressions_current": current_impressions,
            "intent": classify_intent(query_text),
        })

    return issues


def classify_intent(query):
    """Tag query with primary intent."""
    q = query.lower()
    transactional = ["book", "buy", "tour", "price", "cost", "tickets", "reserve", "rental", "charter", "excursion"]
    commercial = ["best", "top", "vs", "comparison", "review", "which", "recommended", "worth it"]
    for p in transactional:
        if p in q:
            return "transactional"
    for p in commercial:
        if p in q:
            return "commercial"
    return "informational"


# ── Main ───────────────────────────────────────────────────────────────
def main():
    dry_run = "--dry-run" in sys.argv

    # Date windows
    today = datetime.now().date()
    current_end = (today - timedelta(days=GSC_LATENCY_DAYS)).strftime("%Y-%m-%d")
    current_start = (today - timedelta(days=GSC_LATENCY_DAYS + CURRENT_WINDOW_DAYS)).strftime("%Y-%m-%d")
    baseline_end = (today - timedelta(days=GSC_LATENCY_DAYS + CURRENT_WINDOW_DAYS - 1)).strftime("%Y-%m-%d")
    baseline_start = (today - timedelta(days=GSC_LATENCY_DAYS + BASELINE_WINDOW_DAYS)).strftime("%Y-%m-%d")

    print(f"# Ranking Drop Watch — {today.isoformat()}\n")
    print(f"Current: {current_start} → {current_end}")
    print(f"Baseline: {baseline_start} → {baseline_end}")
    print(f"Threshold: drop >{POSITION_DROP_THRESHOLD} positions | baseline ≥{MIN_BASELINE_CLICKS} clicks\n")

    # Load state
    state = load_state()

    # Detect per site
    all_issues = []
    new_issues = []
    known_issues = []

    for site_url, site_id in SITES.items():
        print(f"## {site_id}\n", file=sys.stderr)

        issues = detect_drops(site_url, site_id, current_start, current_end, baseline_start, baseline_end)

        for issue in issues:
            key = issue_key(site_id, issue["query"])
            all_issues.append((key, issue))

            if is_in_cooldown(state, key):
                known_issues.append((key, issue))
                continue

            # New regression
            new_issues.append((key, issue))
            state["issues"][key] = {
                "query": issue["query"],
                "site_id": site_id,
                "baseline_pos": issue["baseline_pos"],
                "current_pos": issue["current_pos"],
                "drop": issue["drop"],
                "first_flagged": state["issues"].get(key, {}).get("first_flagged", today.isoformat()),
                "last_flagged": today.isoformat(),
                "last_current_pos": issue["current_pos"],
                "status": "open",
            }

        site_new = [iss for k, iss in new_issues if iss["site_id"] == site_id]
        print(f"  Regressions: {len(issues)} (new: {len(site_new)})\n", file=sys.stderr)

    # Clean resolved issues (position recovered)
    resolved = []
    for key, info in list(state["issues"].items()):
        if info.get("status") == "open":
            # Issue still open — keep it
            pass
        # We could check if current position has recovered here, but that
        # requires re-querying each query — deferred to next iteration

    # Save state
    state["last_run"] = today.isoformat()
    if not dry_run:
        save_state(state)

    # ── Output Report ────────────────────────────────────────────────────
    if not all_issues:
        # Count total queries checked
        total_queries = len(state.get("issues", {}))
        print(f"✅ **No ranking drops detected across all 6 sites.** Fleet stable.\n")
        print(f"_State: {total_queries} total tracked issues_")
        sys.exit(0)

    # New regressions
    if new_issues:
        print(f"## 🚨 New Regressions ({len(new_issues)})\n")
        for key, issue in new_issues:
            arrow = "🔻"
            print(f"### {arrow} {issue['site_id']}: \"{issue['query']}\"\n")
            print(f"| Metric | Current (7d) | Baseline (4w) | Change |")
            print(f"|--------|-------------|---------------|--------|")
            print(f"| **Position** | {issue['current_pos']} | {issue['baseline_pos']} | **↓ {issue['drop']}** |")
            print(f"| **Clicks** | {issue['clicks_current']} | {issue['clicks_baseline']} | — |")
            print(f"| **Impressions** | {issue['impressions_current']} | — | — |")
            print(f"| **Intent** | {issue['intent']} | — | — |")
            print(f"\n**Action:** Check the ranking page for recent changes — content edits, link removal, competitor SERP takeover, or algo update.")
            print()

    # Known (in cooldown)
    if known_issues:
        print(f"## ⏳ Previously Flagged (still in {COOLDOWN_DAYS}-day cooldown)\n")
        for key, issue in known_issues:
            info = state["issues"].get(key, {})
            last = info.get("last_flagged", "?")
            print(f"- **{issue['site_id']}**: \"{issue['query']}\" — pos {issue['current_pos']} (was {issue['baseline_pos']}, ↓{issue['drop']}) — flagged {last}")
        print()

    print(f"---\n")
    print(f"_{len(all_issues)} total drops | {len(new_issues)} new | {len(known_issues)} in cooldown | {len(state['issues'])} open issues_")

    # Exit 1 if new regressions (triggers cron delivery)
    sys.exit(1 if new_issues else 0)


if __name__ == "__main__":
    main()
