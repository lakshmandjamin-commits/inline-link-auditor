#!/usr/bin/env python3
"""
Content drip scheduler: queues new page generation for active sites.
Usage: python3 content_drip.py [site_id]
Checks which sites are due for new pages based on publishing velocity rules.
Outputs a JSON manifest of pages to generate — consumed by build.py.
"""
import sqlite3, os, sys, json, random, yaml
from datetime import datetime, timedelta
from pathlib import Path

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
STATE_FILE = os.path.expanduser("~/.hermes/affiliate-crons/state/content_drip_state.json")
CONTENT_BANKS_DIR = os.path.expanduser("~/.hermes/affiliate-crons/content-banks")

# Velocity rules (pages/day)
VELOCITY = {
    "new": 1,          # days 1-7
    "ramping": 2,       # days 8-30  
    "established": 3,   # day 31+
}
# EEAT multiplier: when site > 30 days AND has named author, allow higher velocity
EEAT_MULTIPLIER_RANGE = (5, 7)  # pages/day if EEAT eligible

def get_sites(target="all"):
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute(
            "SELECT site_id, local_path, domain FROM sites WHERE status='active'").fetchall()
    else:
        sites = reg.execute(
            "SELECT site_id, local_path, domain FROM sites WHERE site_id=? AND status='active'",
            (target,)).fetchall()
    reg.close()
    return sites

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)

def get_site_age(site_id):
    """Calculate days since site was first registered."""
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    row = reg.execute("SELECT created_at FROM sites WHERE site_id=?", (site_id,)).fetchone()
    reg.close()
    if row and row[0]:
        created = datetime.fromisoformat(row[0].replace('Z', '+00:00'))
        return (datetime.now() - created.replace(tzinfo=None)).days
    return 0

def has_named_author(site_id):
    """Check content bank for a named author (EEAT signal)."""
    cb_path = os.path.join(CONTENT_BANKS_DIR, f"{site_id}.yaml")
    if not os.path.exists(cb_path):
        cb_path = os.path.join(CONTENT_BANKS_DIR, f"{site_id}.yml")
    if not os.path.exists(cb_path):
        return False
    try:
        with open(cb_path) as f:
            cb = yaml.safe_load(f)
    except Exception:
        return False
    if not isinstance(cb, dict):
        return False
    voice = cb.get("voice", {})
    author = voice.get("persona_name", "") or voice.get("author", "")
    return bool(author.strip()) if isinstance(author, str) else bool(author)


def get_allowed_pages(site_id):
    """How many pages can this site publish today?
    
    EEAT multiplier: when site > 30 days AND has named author,
    allow 5-7 pages/day vs default 3-5.
    """
    age = get_site_age(site_id)
    if age <= 7:
        return VELOCITY["new"]
    elif age <= 30:
        return VELOCITY["ramping"]
    else:
        # Established site: check EEAT eligibility
        if has_named_author(site_id):
            return random.randint(*EEAT_MULTIPLIER_RANGE)
        return VELOCITY["established"]

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"Content Drip Scheduler — {target} — {datetime.now().isoformat()[:19]}\n")
    
    sites = get_sites(target)
    state = load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    queue = []
    
    for site_id, local_path, domain in sites:
        age = get_site_age(site_id)
        allowed = get_allowed_pages(site_id)
        
        # Check when we last published for this site
        st = state.get(site_id, {})
        last_date = st.get("last_date", "")
        
        # Reset counter if last_date is not today — this must happen BEFORE the
        # capacity check or stale counters (e.g. "published_today: 1" from 3 days ago)
        # will block forever
        if last_date != today:
            st["published_today"] = 0
            st["last_date"] = today

        published_today = st.get("published_today", 0)
        
        if published_today >= allowed:
            print(f"  {site_id}: {published_today}/{allowed} pages today — at capacity")
            continue
        
        # How many more can we publish?
        remaining = allowed - published_today
        tier = "new" if age <= 7 else "ramping" if age <= 30 else "established"
        if age > 30 and has_named_author(site_id):
            tier = "EEAT-accelerated"
        
        print(f"  {site_id}: {age}d old ({tier}), {published_today}/{allowed} published today, {remaining} available")
        
        queue.append({
            "site_id": site_id,
            "local_path": local_path,
            "domain": domain,
            "pages_to_generate": remaining,
            "tier": tier,
            "age_days": age
        })
    
    print(f"\n{'='*50}")
    print(f"Drip queue: {len(queue)} sites, {sum(q['pages_to_generate'] for q in queue)} pages available")
    
    if queue:
        print("\nGeneration manifest:")
        manifest = {"date": today, "queue": queue}
        print(json.dumps(manifest, indent=2))
        # Do NOT increment published_today here — that happened optimistically BEFORE
        # pages were actually generated and caused the counter to be burned by
        # failed cron runs. The deployment step (or cron agent) must update state
        # after successful generation + deploy.
    
    save_state(state)
    
    # Don't exit with error — this is informative
    sys.exit(0)

if __name__ == "__main__":
    main()
