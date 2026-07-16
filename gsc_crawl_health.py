#!/usr/bin/env python3
"""
GSC Crawl Health Monitor — fleet-wide crawl/index health check.
Run weekly. Exit 0=healthy, 1=warnings, 2=critical.

Usage: python3 gsc_crawl_health.py [--verbose]

Alert thresholds:
  - >20% drop in impressions week-over-week per site → degraded
  - >50% drop → critical
  - Sitemap lastDownloaded >7 days ago → degraded
  - Sitemap warnings present → degraded
  - Sitemap errors present → critical
  - Fleet-wide: 0 sites with new impressions in 7 days → critical
"""
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from datetime import datetime, timedelta, timezone
import sys, os, json

CRED_PATH = os.path.expanduser("~/.hermes/credentials/saraswati-gsc.json")
SCOPES = ["https://www.googleapis.com/auth/webmasters", "https://www.googleapis.com/auth/webmasters.readonly"]
STATE_FILE = os.path.expanduser("~/.hermes/affiliate-crons/state/gsc_health_state.json")
SITES = [
    ("sc-domain:madeira-trail-guide.com", "madeira-trail-guide"),
    ("sc-domain:lapland-adventure-guide.com", "lapland-adventure-guide"),
    ("sc-domain:porto-sommelier.com", "porto-sommelier"),
    ("sc-domain:tenerife-outdoor-guide.com", "tenerife-outdoor-guide"),
    ("sc-domain:san-juan-excursions.com", "san-juan-excursions"),
    ("sc-domain:yogyakarta-temple-tours.com", "yogyakarta-temple-tours"),
]
VERBOSE = "--verbose" in sys.argv


def get_service():
    creds = service_account.Credentials.from_service_account_file(CRED_PATH, scopes=SCOPES)
    creds.refresh(Request())
    return build("searchconsole", "v1", credentials=creds)


def query_search_analytics(svc, site_url, start_date, end_date):
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": [],
        "rowLimit": 1,
    }
    resp = svc.searchanalytics().query(siteUrl=site_url, body=body).execute()
    rows = resp.get("rows", [])
    return rows[0] if rows else None


def get_sitemap_status(svc, site_url):
    """Check all sitemaps for a site. Returns (last_downloaded_days_ago, warnings, errors)."""
    try:
        sitemaps = svc.sitemaps().list(siteUrl=site_url).execute()
    except Exception as e:
        return None, 0, 1  # Can't fetch sitemaps = critical

    entries = sitemaps.get("sitemap", [])
    if not entries:
        return None, 0, 1  # No sitemaps = critical

    warnings = 0
    errors = 0
    newest_download = None

    for entry in entries:
        warnings += int(entry.get("warnings", 0))
        errors += int(entry.get("errors", 0))
        dl = entry.get("lastDownloaded")
        if dl:
            dt = datetime.fromisoformat(dl.replace("Z", "+00:00"))
            if newest_download is None or dt > newest_download:
                newest_download = dt

    days_ago = None
    if newest_download:
        days_ago = (datetime.now(timezone.utc) - newest_download).days

    return days_ago, warnings, errors


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
    svc = get_service()
    now = datetime.now()
    # GSC data has 2-3 day latency — end date is 3 days ago
    week_end = (now - timedelta(days=3))
    week_start = (now - timedelta(days=10))
    # Previous week: strictly before week_start, non-overlapping
    prev_end = week_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)
    # Convert to strings for API
    week_end_str = week_end.strftime("%Y-%m-%d")
    week_start_str = week_start.strftime("%Y-%m-%d")
    prev_end_str = prev_end.strftime("%Y-%m-%d")
    prev_start_str = prev_start.strftime("%Y-%m-%d")

    state = load_state()
    alerts = []
    criticals = []
    fleet_new_impressions = 0

    print(f"GSC Crawl Health — {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"  This week: {week_start_str} → {week_end_str}")
    print(f"  Previous:  {prev_start_str} → {prev_end_str}\n")

    for site_url, slug in SITES:
        domain = site_url.replace("sc-domain:", "")
        try:
            this_week = query_search_analytics(svc, site_url, week_start_str, week_end_str)
        except Exception as e:
            print(f"  ❌ {domain}: GSC API error — {e}")
            criticals.append(f"{domain}: GSC API query failed")
            continue
        try:
            prev_week = query_search_analytics(svc, site_url, prev_start_str, prev_end_str)
        except Exception:
            prev_week = None  # Degrade gracefully — can't compute WoW but not critical

        this_imp = this_week["impressions"] if this_week else 0
        prev_imp = prev_week["impressions"] if prev_week else 0
        this_clicks = this_week["clicks"] if this_week else 0

        fleet_new_impressions += this_imp

        # Sitemap health
        dl_days, sm_warnings, sm_errors = get_sitemap_status(svc, site_url)

        status_parts = [f"{domain}: {this_imp} imp / {this_clicks} clicks"]

        # Impression change
        if prev_imp > 0:
            change_pct = ((this_imp - prev_imp) / prev_imp) * 100
            status_parts.append(f"({change_pct:+.0f}% WoW)")
            if change_pct < -50:
                criticals.append(f"{domain}: {change_pct:.0f}% WoW impression drop ({prev_imp} → {this_imp})")
            elif change_pct < -20:
                alerts.append(f"{domain}: {change_pct:.0f}% WoW impression drop ({prev_imp} → {this_imp})")

        # Sitemap issues
        if sm_errors > 0:
            criticals.append(f"{domain}: {sm_errors} sitemap error(s)")
        elif sm_warnings > 0:
            alerts.append(f"{domain}: {sm_warnings} sitemap warning(s)")

        if dl_days is not None and dl_days > 7:
            alerts.append(f"{domain}: sitemap last downloaded {dl_days} days ago")
        elif dl_days is None:
            criticals.append(f"{domain}: sitemap status unavailable")

        print(f"  {'✅' if not any(a.split(':')[0] == domain for a in alerts + criticals) else '⚠️'} {'; '.join(status_parts)}")

    # Fleet-wide check
    if fleet_new_impressions == 0:
        criticals.append("FLEET: zero impressions across all sites — possible GSC outage or de-indexation")

    # Load previous state for trend
    prev_state = state.get("last", {})
    state["last"] = {
        "date": now.isoformat(),
        "fleet_impressions": fleet_new_impressions,
        "alerts": len(alerts) + len(criticals),
    }

    # Trend: 3 consecutive weeks of alerts → escalate
    trend = state.get("trend", [])
    trend.append(len(alerts) + len(criticals))
    if len(trend) > 4:
        trend = trend[-4:]
    state["trend"] = trend

    if len(trend) >= 3 and all(t > 0 for t in trend[-3:]):
        criticals.append(f"TREND: {trend[-3:]} consecutive weeks with alerts — sustained degradation")

    save_state(state)

    print(f"\n{'='*50}")
    if criticals:
        print(f"CRITICAL ({len(criticals)}):")
        for c in criticals:
            print(f"  🔴 {c}")

    if alerts:
        print(f"WARNINGS ({len(alerts)}):")
        for a in alerts:
            print(f"  🟡 {a}")

    if not alerts and not criticals:
        print("✅ Fleet crawl health: HEALTHY")
        sys.exit(0)
    elif criticals:
        sys.exit(2)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
