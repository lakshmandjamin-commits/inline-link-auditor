#!/usr/bin/env python3
"""Fleet GSC report — totals, top queries, and top pages per site.
Uses googleapiclient directly (NOT the Printing Press CLI — it undercounts).

Usage: python3 gsc_fleet_report.py [--days N]
  Default: last 28 days (offset 3 days for GSC data latency)
"""
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import sys, os

CRED_PATH = os.path.expanduser("~/.hermes/credentials/saraswati-gsc.json")
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
SITES = [
    "sc-domain:madeira-trail-guide.com",
    "sc-domain:lapland-adventure-guide.com",
    "sc-domain:porto-sommelier.com",
    "sc-domain:tenerife-outdoor-guide.com",
    "sc-domain:san-juan-excursions.com",
    "sc-domain:yogyakarta-temple-tours.com",
]

# GSC data has 2-3 day latency — end date is 3 days ago
end_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
days = 28
if "--days" in sys.argv:
    idx = sys.argv.index("--days")
    if idx + 1 < len(sys.argv):
        days = int(sys.argv[idx + 1])
start_date = (datetime.now() - timedelta(days=days + 3)).strftime("%Y-%m-%d")

creds = service_account.Credentials.from_service_account_file(CRED_PATH, scopes=SCOPES)
svc = build("searchconsole", "v1", credentials=creds)

def query(site_url, dimensions, row_limit=10):
    body = {"startDate": start_date, "endDate": end_date,
            "dimensions": dimensions, "rowLimit": row_limit}
    return svc.searchanalytics().query(siteUrl=site_url, body=body).execute()

# Totals (dimensions:[] for accurate aggregation)
totals = {}
for site_url in SITES:
    resp = query(site_url, [], 1)
    rows = resp.get("rows", [])
    totals[site_url] = rows[0] if rows else None

print(f"Fleet GSC — {start_date} to {end_date} (last {days} days)\n")

fleet_clicks = fleet_impressions = 0
for site_url in SITES:
    domain = site_url.replace("sc-domain:", "")
    t = totals[site_url]
    if not t:
        print(f"### {domain} — NO DATA\n")
        continue
    fleet_clicks += t["clicks"]
    fleet_impressions += t["impressions"]
    ctr = (t["clicks"] / t["impressions"] * 100) if t["impressions"] else 0
    print(f"### {domain} — {t['clicks']} clicks / {t['impressions']} imp ({ctr:.1f}% CTR) / pos {t['position']:.1f}")

    resp = query(site_url, ["query"], 8)
    if resp.get("rows"):
        print("  Top queries:")
        for r in resp["rows"]:
            print(f"    {r['clicks']:>3}c/{r['impressions']:>5}i  #{r['position']:>4.0f}  \"{r['keys'][0]}\"")

    resp = query(site_url, ["page"], 5)
    if resp.get("rows"):
        print("  Top pages:")
        for r in resp["rows"]:
            page = r['keys'][0].replace(f"https://www.{domain}", "").replace(f"https://{domain}", "")
            print(f"    {r['clicks']:>3}c/{r['impressions']:>5}i  #{r['position']:>4.0f}  {page}")
    print()

fleet_ctr = (fleet_clicks / fleet_impressions * 100) if fleet_impressions else 0
print(f"Fleet total: {fleet_clicks} clicks / {fleet_impressions} imp ({fleet_ctr:.1f}% CTR)")
