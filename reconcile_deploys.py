#!/usr/bin/env python3
"""
Reconciler — Deploy Gate Backstop (Layer 3 defense-in-depth).

Detects Vercel production deploys that reached production without a
corresponding ledger entry (bypassing the deploy pipeline).

Usage:
  python3 reconcile_deploys.py            # Live run
  python3 reconcile_deploys.py --dry-run  # Check only, no alerts
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERMES_DIR = Path.home() / ".hermes"
AFFILIATE_DIR = HERMES_DIR / "affiliate-crons"
SCRIPTS_DIR = AFFILIATE_DIR / "scripts"
STATE_DIR = AFFILIATE_DIR / "state"
CONFIG_DIR = AFFILIATE_DIR / "config"
LEDGER_PATH = STATE_DIR / "deploy_ledger.json"
ALERT_STATE_PATH = STATE_DIR / "reconciler_alert_state.json"
GATE_CONFIG_PATH = CONFIG_DIR / "gate-config.json"

DRY_RUN = "--dry-run" in sys.argv


def load_json(path):
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"  ⚠️ {path}: JSON parse error — {e}", file=sys.stderr)
            return {}
    return {}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_gate_config():
    cfg = load_json(GATE_CONFIG_PATH)
    utility_raw = cfg.get("utility_pages", [])
    # Build set of basenames from utility_pages (handle both .html and extensionless)
    utility = set()
    for p in utility_raw:
        utility.add(p)
        if p.endswith(".html"):
            utility.add(p.replace(".html", ""))
    return {
        "antiwords_en": "|".join(cfg.get("antiwords", {}).get("en", [])),
        "antiwords_de": "|".join(cfg.get("antiwords", {}).get("de", [])),
        "antiwords_es": "|".join(cfg.get("antiwords", {}).get("es", [])),
        "min_words": cfg.get("thresholds", {}).get("min_words_editorial", 400),
        "utility_pages": utility,
    }


def get_sites():
    import sqlite3
    db = AFFILIATE_DIR / "db" / "site_registry.db"
    if not db.exists():
        sites_dir = Path.home() / "sites"
        sites = []
        if sites_dir.exists():
            for d in sorted(sites_dir.iterdir()):
                if d.is_dir() and (d / ".git").exists():
                    sites.append({"site_id": d.name, "local_path": str(d), "domain": None})
        return sites
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT site_id, local_path, domain FROM sites WHERE status='active'"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_deployment(site_slug=None, domain=None):
    """Fetch latest READY production deployment from Vercel API."""
    try:
        project = site_slug
        result = subprocess.run(
            ["npx", "vercel", "ls", project, "--environment", "production", "--limit", "5"],
            capture_output=True, text=True, timeout=30,
            cwd=Path.home(),
        )
        if result.returncode != 0:
            # npx vercel unavailable — fall through to API fallback
            pass
        for line in result.stdout.splitlines():
            if "dpl_" in line and "Ready" in line:
                parts = line.split()
                for i, part in enumerate(parts):
                    if "dpl_" in part:
                        return {"deploy_id": part, "state": "READY", "url": parts[-1] if parts else None}
        # Vercel API fallback
        token = os.environ.get("VERCEL_TOKEN", "")
        if token:
            import urllib.request
            url = f"https://api.vercel.com/v6/deployments?projectId={project}&limit=5&state=READY&target=production"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                for d in data.get("deployments", []):
                    if d.get("state") == "READY":
                        return {"deploy_id": d["uid"], "state": "READY", "url": d.get("url")}
        return None
    except Exception as e:
        print(f"  ⚠️ Vercel API error: {e}", file=sys.stderr)
        return None


def check_gates(local_path, gate_config):
    """Run gate checks on a site's sitemap pages. Returns findings (quality FYI)."""
    sitemap = Path(local_path) / "sitemap.xml"
    if not sitemap.exists():
        return {"sitemap": "MISSING", "pages_checked": 0, "findings": []}

    findings = []
    try:
        import xml.etree.ElementTree as ET
        ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        root = ET.parse(str(sitemap)).getroot()
        urls = [loc.text for loc in root.findall(".//ns:loc", ns)]
    except Exception as e:
        return {"sitemap": f"PARSE ERROR: {e}", "pages_checked": 0, "findings": []}

    anti_en = gate_config["antiwords_en"]
    anti_de = gate_config["antiwords_de"]
    anti_es = gate_config["antiwords_es"]
    min_words = gate_config["min_words"]
    utility = gate_config["utility_pages"]

    pages_checked = 0
    for url in urls:
        # Convert URL to local file path
        try:
            path = "/" + url.split("/", 3)[-1] if "/" in url.split("//", 1)[-1] else "/index.html"
        except Exception:
            path = "/index.html"
        if path.endswith("/"):
            path += "index.html"

        basename = os.path.basename(path.rstrip("/"))
        parent = os.path.basename(os.path.dirname(path.rstrip("/")))

        if basename in utility or parent in utility:
            continue

        file_path = Path(local_path) / path.lstrip("/")
        # Clean URLs: /about → try about/index.html or about.html
        if not file_path.exists() or not file_path.is_file():
            # Try index.html inside directory
            alt = file_path / "index.html"
            if alt.exists() and alt.is_file():
                file_path = alt
            else:
                # Try .html suffix
                alt2 = Path(str(file_path) + ".html")
                if alt2.exists() and alt2.is_file():
                    file_path = alt2
                else:
                    findings.append(f"⚠️ {path}: 404 locally (in sitemap, not on disk)")
                    continue

        # Security: resolved path must stay within site directory
        site_root = Path(local_path).resolve()
        resolved = file_path.resolve()
        try:
            resolved.relative_to(site_root)
        except ValueError:
            findings.append(f"⚠️ {path}: path escapes site directory — skipped")
            continue

        pages_checked += 1
        content = file_path.read_text()

        lang = "en"
        if "/de/" in str(path):
            lang = "de"
        elif "/es/" in str(path):
            lang = "es"

        # Word count
        body = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL)
        body = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", body)
        wc = len(text.split())
        if wc < min_words:
            findings.append(f"THIN: {path}: {wc} words (< {min_words})")

        # Viator links
        if "viator.com" not in content:
            findings.append(f"ZERO LINKS: {path}")

        # Anti-words
        if lang == "de":
            anti = anti_de
        elif lang == "es":
            anti = anti_es
        else:
            anti = anti_en

        if anti:
            stripped = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL)
            stripped = re.sub(r"<style[^>]*>.*?</style>", "", stripped, flags=re.DOTALL)
            stripped = re.sub(r"<!--.*?-->", "", stripped, flags=re.DOTALL)
            stripped = re.sub(r"<meta[^>]*>", "", stripped)
            pattern = re.compile(r"\b(" + anti + r")\b", re.IGNORECASE)
            matches = pattern.findall(stripped)
            if matches:
                unique = sorted(set(m.lower() for m in matches))
                findings.append(f"ANTI-WORDS: {path}: {len(matches)} hit(s) — {', '.join(unique[:5])}")

    return {"sitemap": f"{len(urls)} URLs", "pages_checked": pages_checked, "findings": findings}


def main():
    print("━━━ RECONCILER — Deploy Gate Backstop ━━━")
    print(f"  {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print(f"  {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print()

    ledger = load_json(LEDGER_PATH)
    gate_config = load_gate_config()
    sites = get_sites()

    if not sites:
        print("❌ No sites found")
        return 2

    bypasses = []
    quality_all = {}
    fetch_failures = 0

    for site in sites:
        sid = site["site_id"]
        lpath = site["local_path"]

        if not Path(lpath).exists():
            print(f"❌ {sid}: local path missing")
            continue

        deploy = get_latest_deployment(site_slug=sid)
        if not deploy:
            print(f"⚠️ {sid}: could not fetch deployment")
            fetch_failures += 1
            continue

        did = deploy["deploy_id"]
        # Ledger is a flat list of deploy entries — filter by site
        if isinstance(ledger, list):
            site_deploys = [e for e in ledger if e.get("site") == sid]
        else:
            site_deploys = ledger.get(sid, {}).get("deploys", [])
        known = {e.get("deploy_id", "") for e in site_deploys}

        if not known:
            print(f"⚠️ {sid}: no ledger — seeding baseline {did}")
            if not DRY_RUN:
                entry = {
                    "site": sid,
                    "deploy_id": did, "baseline": True,
                    "note": "Auto-seeded baseline",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                if isinstance(ledger, list):
                    ledger.append(entry)
                else:
                    ledger.setdefault(sid, {})["deploys"] = [entry]
                save_json(LEDGER_PATH, ledger)

        # Quality check — runs regardless
        q = check_gates(lpath, gate_config)
        if q["findings"]:
            quality_all[sid] = q["findings"]
            for f in q["findings"]:
                print(f"  📋 {f}")
        else:
            print(f"  ✅ {q['pages_checked']} pages clean")

        # Bypass decision: unknown deploy → check quality first
        if did not in known:
            # NEW: auto-seed clean deploys (content-drip auto-deploys from git push)
            if not q["findings"]:
                print(f"  🔧 {sid}: auto-seeding clean deploy {did} (content-drip auto-deploy)")
                if not DRY_RUN:
                    entry = {
                        "site": sid,
                        "deploy_id": did,
                        "note": "Auto-seeded — clean quality, content-drip auto-deploy",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    if isinstance(ledger, list):
                        ledger.append(entry)
                    else:
                        ledger.setdefault(sid, {})["deploys"] = ledger.get(sid, {}).get("deploys", []) + [entry]
                    save_json(LEDGER_PATH, ledger)
            else:
                # Real bypass — unknown deploy WITH quality issues
                print(f"🚨 {sid}: BYPASS — {did} not in ledger (quality issues detected)")
                bypasses.append({"site": sid, "deploy_id": did})
        else:
            print(f"✅ {sid}: {did} — ledger OK")

        print()

    if bypasses:
        print("❌ RECONCILER: Gate bypass(es) detected!")
        return 1
    if fetch_failures == len(sites):
        print("❌ RECONCILER: All sites failed to fetch — possible Vercel outage")
        return 2
    if quality_all:
        print("⚠️ RECONCILER: Quality findings (FYI) but no bypasses.")
        return 0
    print("✅ RECONCILER: All sites healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
