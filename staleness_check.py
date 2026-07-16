#!/usr/bin/env python3
"""
Content staleness: flag pages not updated in 90+ days.
Usage: python3 staleness_check.py [all|site_id]
Detects pages that haven't been touched — candidates for refresh or retirement.
"""
import sqlite3, os, sys, re
from datetime import datetime, timedelta
from pathlib import Path
DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
STALE_DAYS = 90
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
def extract_date_from_html(html, page_path):
   """Extract last-updated date from page content."""
   # Try meta tags first
   og_updated = re.search(r'<meta[^>]*property="[^"]*modified_time"[^>]*content="([^"]*)"', html)
   if og_updated:
       return og_updated.group(1)
   
   article_modified = re.search(r'<meta[^>]*property="article:modified_time"[^>]*content="([^"]*)"', html)
   if article_modified:
       return article_modified.group(1)
   
   # Try visible "Last updated" text
   last_updated = re.search(r'Last updated:?\s*([A-Z][a-z]+ \d{4}|\d{4}-\d{2}-\d{2})', html, re.IGNORECASE)
   if last_updated:
       return last_updated.group(1)
   
   # Try footer copyright
   copyright = re.search(r'© \d{4}.*?(?:Last updated:?\s*([^<]+))', html, re.IGNORECASE)
   if copyright:
       return copyright.group(1).strip()
   
   # Fall back to file modification time
   return None
def get_file_age(filepath):
   """Get days since last modification."""
   mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
   return (datetime.now() - mtime).days
def main():
   target = sys.argv[1] if len(sys.argv) > 1 else "all"
   print(f"Content Staleness Check — {target} — {datetime.now().isoformat()[:19]}\n")
   
   sites = get_sites(target)
   all_stale = []
   
   for site_id, local_path, domain in sites:
       print(f"--- {site_id} ---")
       site_path = Path(local_path)
       pages = sorted(site_path.rglob("*.html"))
       stale = []
       fresh = 0
       
       for page in pages:
           if "node_modules" in str(page):
               continue
           
           html = page.read_text(errors="ignore")
           date_str = extract_date_from_html(html, page)
           age = get_file_age(page)
           
           if age > STALE_DAYS:
               rel = page.relative_to(site_path)
               stale.append((str(rel), age, date_str or "unknown"))
           else:
               fresh += 1
       
       if stale:
           print(f"  STALE: {len(stale)} pages (>{STALE_DAYS} days), {fresh} fresh")
           for rel, age, date_str in stale[:10]:
               print(f"    {rel:<45} {age:>4}d ago  (last updated: {date_str})")
               all_stale.append(f"{site_id}: {rel} — {age}d stale")
           if len(stale) > 10:
               print(f"    ... and {len(stale)-10} more")
       else:
           print(f"  ALL {fresh} pages fresh (<{STALE_DAYS} days)")
       
       # Log
       try:
           reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
           reg.execute(
               "INSERT INTO audit_log (site_id, check_type, details) VALUES (?, 'staleness_check', ?)",
               (site_id, f"{len(stale)} stale of {len(pages)} pages"))
           reg.commit()
           reg.close()
       except Exception as e:
           print(f"  [WARN] DB update failed: {e}", file=sys.stderr)
       print()
   
   print(f"{'='*50}")
   print(f"Staleness Check complete: {len(all_stale)} stale pages across all sites")
   if all_stale:
       print("\nAll stale pages:")
       for s in all_stale[:15]:
           print(f"  -> {s}")
   
   # Exit with warning if any stale
   sys.exit(1 if len(all_stale) > 0 else 0)
if __name__ == "__main__":
   main()
