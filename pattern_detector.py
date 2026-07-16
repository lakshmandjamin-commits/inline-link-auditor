#!/usr/bin/env python3
"""
Fleet pattern detector: cross-site error pattern analysis.
Usage: python3 pattern_detector.py
Looks for the same issue appearing across multiple sites — systemic problems.
"""
import sqlite3, os, sys, json, re
from datetime import datetime
from collections import Counter
DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
def main():
   print(f"Fleet Pattern Detector — {datetime.now().isoformat()[:19]}\n")
   
   reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
   
   # Get all audit log entries from the last 30 days
   logs = reg.execute("""
       SELECT site_id, check_type, details, run_at 
       FROM audit_log 
       WHERE run_at > datetime('now', '-30 days')
       ORDER BY run_at DESC
   """).fetchall()
   
   if not logs:
       print("No audit log entries found in last 30 days.")
       reg.close()
       return
   
   # Group by event type
   events = Counter()
   for _, event, _, _ in logs:
       events[event] += 1
   
   print("Event frequency (last 30 days):")
   for event, count in events.most_common():
       print(f"  {event:<25} {count:>4}")
   
   # Detect patterns: same event on multiple sites
   print("\nCross-site patterns:")
   event_sites = {}
   for site_id, event, detail, _ in logs:
       event_sites.setdefault(event, set()).add(site_id)
   
   patterns_found = False
   for event, sites in sorted(event_sites.items()):
       if len(sites) >= 2:
           patterns_found = True
           print(f"  PATTERN: '{event}' occurring on {len(sites)} sites: {', '.join(sorted(sites))}")
   
   if not patterns_found:
       print("  No cross-site patterns detected (good).")
   
   # Site health summary — count pages from sitemaps (always current)
   print("\nSite health summary:")
   sites = reg.execute("SELECT site_id, status, local_path, last_validated_at, last_link_check_at FROM sites").fetchall()
   for site_id, status, local_path, validated, links in sites:
       health = "✓"
       issues = []
       
       # Count pages from sitemap (canonical), fall back to HTML file count
       pages = 0
       sitemap_path = os.path.join(local_path, 'sitemap.xml')
       if not os.path.exists(sitemap_path):
           # Handle nested site structures (e.g. madeira)
           sitemap_path = os.path.join(os.path.dirname(local_path), 'sitemap.xml')
       if os.path.exists(sitemap_path):
           with open(sitemap_path) as f:
               pages = len(re.findall(r'<loc>', f.read()))
       else:
           # Fallback: count HTML files
           html_count = 0
           for root, dirs, files in os.walk(local_path):
               dirs[:] = [d for d in dirs if d not in {'.git','node_modules','backup'} and not d.startswith('.')]
               html_count += sum(1 for f in files if f.endswith('.html'))
           pages = html_count
       
       if status != 'active':
           health = "⚠️"
           issues.append(f"status={status}")
       if not validated:
           issues.append("never validated")
       if not links:
           issues.append("never link-checked")
       
       print(f"  {health} {site_id:<30} {pages or '?'} pages | validated: {validated or 'never'} | links: {links or 'never'}")
       if issues:
           print(f"     Issues: {', '.join(issues)}")
   
   reg.close()
   print(f"\n{'='*50}")
   print("Pattern detection complete.")
if __name__ == "__main__":
   main()
