#!/usr/bin/env python3
"""
Deploy — pushes approved articles to site repos. Vercel auto-deploys on git push.

Usage: python3 deploy.py [site_slug|--all]
  Reads approved articles from ~/.hermes/affiliate-crons/approved/{slug}/
  Copies to site's local_path/{slug}/index.html
  Git add, commit, push
"""
import sys, os, sqlite3, shutil, re, subprocess
from datetime import datetime
from pathlib import Path

APPROVED_DIR = os.path.expanduser("~/.hermes/affiliate-crons/approved")
DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
SCRIPTS_DIR = os.path.expanduser("~/.hermes/affiliate-crons/scripts")
DEPLOY_STATE_FILE = os.path.expanduser("~/.hermes/affiliate-crons/state/deploy_state.json")


def get_sites(target="all"):
    """Get active sites from the registry."""
    reg = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
    if target == "all":
        sites = reg.execute(
            "SELECT site_id, local_path, domain FROM sites WHERE status='active'"
        ).fetchall()
    else:
        sites = reg.execute(
            "SELECT site_id, local_path, domain FROM sites WHERE site_id=? AND status='active'",
            (target,)
        ).fetchall()
    reg.close()
    return sites


def get_approved_articles(site_slug):
    """List approved articles not yet deployed."""
    site_dir = os.path.join(APPROVED_DIR, site_slug)
    if not os.path.exists(site_dir):
        return []

    import json
    state = {}
    if os.path.exists(DEPLOY_STATE_FILE):
        with open(DEPLOY_STATE_FILE) as f:
            state = json.load(f)

    deployed = set(state.get(site_slug, {}).get("deployed", []))
    articles = []
    for f in sorted(os.listdir(site_dir)):
        if f.endswith(".html"):
            slug = f.replace(".html", "")
            if slug not in deployed:
                articles.append((slug, os.path.join(site_dir, f)))
    return articles


def inject_site_elements(html, domain, article_slug):
    """Add site-specific elements: canonical URL, OG tags, CSS path fix."""
    # Fix CSS path if needed
    html = html.replace('href="/css/style.css"', 'href="/css/style.css"')

    # Add canonical URL — always force www. prefix
    safe_domain = domain if domain.startswith('www.') else f'www.{domain}'
    canonical = f'<link rel="canonical" href="https://{safe_domain}/{article_slug}">'
    if '<link rel="canonical"' not in html:
        html = html.replace('<meta charset="UTF-8">',
                           f'<meta charset="UTF-8">\n  {canonical}')

    # Add OG tags if missing
    if '<meta property="og:title"' not in html:
        title_match = re.search(r'<title>(.*?)</title>', html)
        desc_match = re.search(r'<meta name="description" content="([^"]+)"', html)
        title = title_match.group(1) if title_match else article_slug
        desc = desc_match.group(1) if desc_match else title

        og_tags = f"""<meta property="og:title" content="{title}">
  <meta property="og:description" content="{desc}">
  <meta property="og:type" content="website">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{title}">"""

        # Insert before </head>
        html = html.replace('</head>', f'\n{og_tags}\n</head>')

    return html


def git_push(local_path, commit_msg):
    """Add all changes, commit, and push."""
    import subprocess

    try:
        subprocess.run(["git", "add", "."], cwd=local_path, capture_output=True, check=True)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=local_path,
                               capture_output=True, text=True, check=True)
        if not status.stdout.strip():
            return False, "No changes to commit"

        subprocess.run(["git", "commit", "-m", commit_msg], cwd=local_path,
                      capture_output=True, check=True)
        result = subprocess.run(["git", "push"], cwd=local_path, capture_output=True,
                               text=True, timeout=30)
        if result.returncode != 0:
            return False, result.stderr or "Push failed"
        return True, "Pushed successfully"
    except subprocess.CalledProcessError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def trigger_vercel_deploy(site_slug):
    """Trigger a Vercel deploy via REST API. Works even when git webhooks are broken."""
    import json
    import urllib.request
    import urllib.error

    config_path = os.path.expanduser("~/.hermes/vercel_deploy_config.json")
    if not os.path.exists(config_path):
        return False, "No vercel_deploy_config.json found"

    with open(config_path) as f:
        config = json.load(f)

    cfg = config.get(site_slug)
    if not cfg:
        return False, f"Site '{site_slug}' not in vercel config"

    vercel_token = os.environ.get("VERCEL_TOKEN", "")
    if not vercel_token:
        return False, "VERCEL_TOKEN not set"

    body = json.dumps({
        "name": cfg["github_repo"],
        "target": "production",
        "gitSource": {
            "type": "github",
            "org": cfg["github_org"],
            "repo": cfg["github_repo"],
            "repoId": cfg["github_repo_id"],
            "ref": cfg["ref"],
        }
    }).encode()

    try:
        req = urllib.request.Request(
            "https://api.vercel.com/v13/deployments?forceNew=1",
            data=body,
            headers={
                "Authorization": f"Bearer {vercel_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            dep = json.load(r)
        dep_id = dep.get("id", dep.get("uid", "unknown"))
        state = dep.get("state", "unknown")
        return True, f"Deploy {dep_id} state={state}"
    except urllib.error.HTTPError as e:
        return False, f"Vercel API error {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return False, str(e)


def deploy_site(site_slug, local_path, domain):
    """Deploy all approved articles for a site."""
    articles = get_approved_articles(site_slug)
    if not articles:
        return 0, "No new articles to deploy"

    deployed = []
    for article_slug, src_path in articles:
        # Create target directory
        dest_dir = os.path.join(local_path, article_slug)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, "index.html")

        # Read, inject site elements, write
        with open(src_path) as f:
            html = f.read()

        html = inject_site_elements(html, domain, article_slug)

        with open(dest_path, "w") as f:
            f.write(html)

        deployed.append(article_slug)
        print(f"  {article_slug} → {article_slug}/index.html")

    # Git operations
    commit_msg = f"deploy: {len(deployed)} new article(s) — {', '.join(deployed[:3])}"
    if len(deployed) > 3:
        commit_msg += f" +{len(deployed)-3} more"

    success, msg = git_push(local_path, commit_msg)
    if not success:
        print(f"  Git error: {msg}")
        return 0, f"Git push failed: {msg}"

    # ── GSC Sitemap Submit (Phase 3: auto-indexing signal) ──
    try:
        gsc_script = os.path.join(SCRIPTS_DIR, "gsc_sitemap_submit.py")
        if os.path.exists(gsc_script):
            result = subprocess.run(
                [sys.executable, gsc_script, site_slug],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                print("  GSC: Sitemap submitted for re-crawl")
            else:
                print(f"  GSC: Sitemap submit issue — {result.stderr[:150]}")
    except Exception as e:
        print(f"  GSC: Sitemap submit skipped — {e}")

    # Trigger Vercel deploy — git webhooks are broken (credential expired)
    vercel_ok, vercel_msg = trigger_vercel_deploy(site_slug)
    if vercel_ok:
        print(f"  Vercel: {vercel_msg}")
    else:
        print(f"  Vercel WARNING: {vercel_msg}")

    # Update deploy state
    import json
    state = {}
    if os.path.exists(DEPLOY_STATE_FILE):
        with open(DEPLOY_STATE_FILE) as f:
            state = json.load(f)

    site_state = state.get(site_slug, {})
    site_state.setdefault("deployed", []).extend(deployed)
    state[site_slug] = site_state

    os.makedirs(os.path.dirname(DEPLOY_STATE_FILE), exist_ok=True)
    with open(DEPLOY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

    return len(deployed), msg


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"Deploy — {target} — {datetime.now().isoformat()[:19]}\n")

    sites = get_sites(target)
    if not sites:
        print("No active sites found.")
        sys.exit(0)

    total = 0
    for site_slug, local_path, domain in sites:
        if not os.path.exists(local_path):
            print(f"  SKIP {site_slug}: local_path not found — {local_path}")
            continue

        print(f"{site_slug} ({domain}):")
        count, msg = deploy_site(site_slug, local_path, domain)

        if count > 0:
            print(f"  ✅ {count} deployed — {msg}")
            total += count
        else:
            print(f"  ℹ️  {msg}")

    print(f"\nTotal: {total} articles deployed.")


if __name__ == "__main__":
    main()
