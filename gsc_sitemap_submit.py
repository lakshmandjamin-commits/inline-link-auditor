#!/usr/bin/env python3
"""
GSC Sitemap Submitter — regenerate + submit sitemap after content deploy.
Hooks into content_drip_orchestrator.py and deploy.py post-push.

Usage: python3 gsc_sitemap_submit.py <site_slug> [--dry-run] [--no-regen]
  Regenerates sitemap for site_slug, submits to GSC, logs to state file.
  --no-regen: skip regeneration (caller already regenerated via sitemap-generator.py)

Quota: GSC allows frequent sitemap submissions. Script is safe to run on every deploy.
"""
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from datetime import datetime, timezone
import sys, os, json, subprocess, sqlite3

CRED_PATH = os.path.expanduser("~/.hermes/credentials/saraswati-gsc.json")
SCOPES = ["https://www.googleapis.com/auth/webmasters", "https://www.googleapis.com/auth/webmasters.readonly"]
SCRIPTS_DIR = os.path.expanduser("~/.hermes/affiliate-crons/scripts")
DB_PATH = os.path.expanduser("~/.hermes/affiliate-crons/db/site_registry.db")
STATE_FILE = os.path.expanduser("~/.hermes/affiliate-crons/state/gsc_sitemap_submissions.json")
DRY_RUN = "--dry-run" in sys.argv
NO_REGEN = "--no-regen" in sys.argv


def get_service():
    creds = service_account.Credentials.from_service_account_file(CRED_PATH, scopes=SCOPES)
    creds.refresh(Request())
    return build("searchconsole", "v1", credentials=creds)


def get_domain(site_slug):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT domain FROM sites WHERE site_id=? AND status='active'",
        (site_slug,)
    ).fetchone()
    conn.close()
    if not row:
        print(f"ERROR: Site '{site_slug}' not found or not active")
        sys.exit(1)
    return row[0]


def regenerate_sitemap(site_slug):
    """Regenerate sitemap for this site. Returns (success, local_path)."""
    script = os.path.join(SCRIPTS_DIR, "sitemap-generator.py")
    if not os.path.exists(script):
        print(f"ERROR: sitemap-generator.py not found at {script}")
        return False, None

    # Look up site directory and domain from registry
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT local_path, domain FROM sites WHERE site_id=? AND status='active'",
        (site_slug,)
    ).fetchone()
    conn.close()
    if not row:
        print(f"ERROR: Site '{site_slug}' not found or not active")
        return False, None

    local_path, domain = row

    result = subprocess.run(
        [sys.executable, script, "--dir", local_path, "--domain", domain],
        capture_output=True, text=True, timeout=30, cwd=SCRIPTS_DIR
    )
    if result.returncode != 0:
        print(f"ERROR: Sitemap generation failed for {site_slug}")
        print(result.stderr[:500] if result.stderr else result.stdout[:500])
        return False, None
    return True, local_path


def submit_sitemap(svc, site_url, sitemap_path):
    """Submit a sitemap to GSC."""
    if DRY_RUN:
        print(f"  [DRY RUN] Would submit {sitemap_path}")
        return True

    try:
        svc.sitemaps().submit(
            siteUrl=site_url,
            feedpath=sitemap_path
        ).execute()
        return True
    except Exception as e:
        print(f"  WARNING: Sitemap submit failed: {e}")
        return False


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def main():
    if len(sys.argv) < 2 or sys.argv[1].startswith("--"):
        print("Usage: python3 gsc_sitemap_submit.py <site_slug> [--dry-run]")
        sys.exit(1)

    site_slug = sys.argv[1]
    domain = get_domain(site_slug)
    site_url = f"sc-domain:{domain}"

    print(f"GSC Sitemap Submit — {site_slug} ({domain}) — {datetime.now().isoformat()[:19]}")

    # 1. Regenerate sitemap (skip if caller already did it)
    if NO_REGEN:
        print("  ⏭️ Skipping sitemap regen (--no-regen: caller already regenerated)")
    else:
        print("  Regenerating sitemap...")
        ok, local_path = regenerate_sitemap(site_slug)
        if not ok:
            sys.exit(1)
        print("  ✅ Sitemap regenerated")

    # 2. Submit to GSC
    svc = get_service()
    sitemap_paths = [
        f"https://www.{domain}/sitemap.xml",
        f"https://{domain}/sitemap.xml",
    ]

    submitted = 0
    for path in sitemap_paths:
        if submit_sitemap(svc, site_url, path):
            submitted += 1
            print(f"  ✅ Submitted: {path}")

    # 3. Log submission
    state = load_state()
    site_state = state.get(site_slug, {"submissions": []})
    site_state["submissions"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sitemaps": [p for p in sitemap_paths if submitted > 0],
    })
    # Keep only last 50 entries
    if len(site_state["submissions"]) > 50:
        site_state["submissions"] = site_state["submissions"][-50:]
    state[site_slug] = site_state
    save_state(state)

    if submitted > 0:
        print(f"\n✅ {submitted} sitemap(s) submitted. Google will recrawl within 1-3 days.")
        sys.exit(0)
    else:
        print("\n⚠️ No sitemaps submitted successfully")
        sys.exit(1)


if __name__ == "__main__":
    main()
