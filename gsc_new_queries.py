#!/usr/bin/env python3
"""GSC Emerging Query Detection — flags queries new in the last 7 days 
that weren't in the prior 28-day baseline (offset 3 days for data latency).

Gate A correction: baseline = days 10-37 prior (NOT days 1-28 including detection window).
Detection window = days 3-10 prior (offset for 2-3 day GSC data lag)."""

from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import os, sys

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

def query(svc, site_url, start, end, dimensions, row_limit=5000):
    body = {"startDate": start, "endDate": end, "dimensions": dimensions, "rowLimit": row_limit}
    return svc.searchanalytics().query(siteUrl=site_url, body=body).execute()

def main():
    creds = service_account.Credentials.from_service_account_file(CRED_PATH, scopes=SCOPES)
    svc = build("searchconsole", "v1", credentials=creds)
    
    today = datetime.now()
    # Detection window: days 3-10 ago (offset for GSC 2-3 day latency)
    det_end = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    det_start = (today - timedelta(days=10)).strftime("%Y-%m-%d")
    # Baseline window: days 10-37 ago (PRIOR to detection, no overlap)
    base_end = (today - timedelta(days=10)).strftime("%Y-%m-%d")
    base_start = (today - timedelta(days=37)).strftime("%Y-%m-%d")

    print(f"Detection: {det_start} → {det_end}")
    print(f"Baseline:  {base_start} → {base_end}")
    print()

    total_new = 0
    for site_url in SITES:
        domain = site_url.replace("sc-domain:", "")
        
        # Get detection window queries
        det = query(svc, site_url, det_start, det_end, ["query"])
        det_queries = {r["keys"][0]: r for r in det.get("rows", [])}
        
        # Get baseline queries (28 days prior)
        base = query(svc, site_url, base_start, base_end, ["query"])
        base_queries = {r["keys"][0] for r in base.get("rows", [])}
        
        # Find queries in detection that are NOT in baseline
        new_queries = []
        for q, r in det_queries.items():
            if q not in base_queries and r["impressions"] >= 2:
                new_queries.append((q, r["impressions"], r["clicks"], r["position"]))
        
        if new_queries:
            new_queries.sort(key=lambda x: -x[1])
            print(f"=== {domain} — {len(new_queries)} new queries ===")
            for q, imps, clicks, pos in new_queries[:10]:
                print(f"  {clicks:>2}c/{imps:>4}i  pos {pos:>5.1f}  \"{q}\"")
            total_new += len(new_queries)
    
    print(f"\nTotal new queries fleet-wide: {total_new}")
    return 0 if total_new == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
