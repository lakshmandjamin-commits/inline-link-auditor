#!/usr/bin/env python3
"""
Content Drip Orchestrator — deterministic no_agent replacement for LLM-driven drip crons.
Runs the full Phase 1-5 pipeline: filter → dedup → generate → QA → deploy → state update.

Usage: python3 content_drip_orchestrator.py <site_slug>

Design: no_agent cron script. Stdout IS the user-facing report. Exit 0 = success
(possibly with nothing to do), exit 1 = hard failure needing attention.
"""
import sys, os, json, subprocess, sqlite3, shutil, time, re, yaml, itertools
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path.home() / ".hermes" / "affiliate-crons" / "scripts"
BRIEFS_DIR = Path.home() / ".hermes" / "affiliate-crons" / "briefs"
STATE_FILE = Path.home() / ".hermes" / "affiliate-crons" / "state" / "generation_state.json"
DB_PATH = Path.home() / ".hermes" / "affiliate-crons" / "db" / "site_registry.db"
GENERATED_DIR = Path.home() / ".hermes" / "affiliate-crons" / "generated"
ENV_FILE = Path.home() / ".hermes" / ".env"


def load_site(site_slug):
    """Load site config from registry."""
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute(
        "SELECT site_id, local_path, domain FROM sites WHERE site_id=? AND status='active'",
        (site_slug,)
    ).fetchone()
    conn.close()
    if not row:
        print(f"ERROR: Site '{site_slug}' not found or not active in registry")
        sys.exit(1)
    return {"site_id": row[0], "local_path": row[1], "domain": row[2]}


def source_env():
    """Source the .env file and return a dict of env vars for subprocess."""
    env = os.environ.copy()
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    env[key] = val
    # Unset GIT env vars to prevent cross-repo contamination
    for git_var in ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"]:
        env.pop(git_var, None)
    return env


def run_script(script_name, args, env, timeout=120):
    """Run a script, return (exit_code, stdout, stderr)."""
    script_path = SCRIPTS_DIR / script_name
    cmd = [sys.executable, str(script_path)] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                env=env, timeout=timeout, cwd=str(SCRIPTS_DIR))
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"TIMEOUT after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


def load_state():
    """Load generation state."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    """Save generation state atomically."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(STATE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, str(STATE_FILE))


def report(msg):
    """Print report line — captured as cron stdout."""
    print(msg)


def main():
    if len(sys.argv) < 2:
        print("Usage: content_drip_orchestrator.py <site_slug>")
        sys.exit(1)

    site_slug = sys.argv[1]
    site = load_site(site_slug)
    env = source_env()
    tour_review_slugs_allowed = None  # Set in Phase 1.7 if comparison pool is low

    report(f"# Content Drip — {site['domain']} ({site_slug})")
    report(f"*{datetime.now().strftime('%Y-%m-%d %H:%M')}*")

    # ── Phase 1: Filter briefs ──
    report("\n## Phase 1 — Filter Briefs")
    rc, out, err = run_script("filter_briefs.py", [site_slug], env, timeout=60)

    if rc != 0:
        report(f"❌ filter_briefs.py failed (exit {rc})")
        if err:
            report(f"```\n{err[:500]}\n```")
        sys.exit(1)

    # Count available briefs
    filtered_path = f"/tmp/filtered-{site_slug}.json"
    if not os.path.exists(filtered_path):
        report("⚠️ No filtered briefs file produced — nothing to generate")
        return  # exit 0 = silent/success, no work to do

    with open(filtered_path) as f:
        filtered_data = json.load(f)

    briefs = filtered_data.get("briefs", [])
    if not briefs:
        report("ℹ️ No briefs available for this site — nothing to generate")
        return

    report(f"Found {len(briefs)} filtered briefs")

    # ── Phase 1.5: Verify brief product codes against Viator DB ──
    report("\n## Phase 1.5 — Verify Products")
    verify_script = SCRIPTS_DIR / "verify_brief_products.py"
    if verify_script.exists():
        # Verify against the SOURCE briefs file (not /tmp/filtered), since
        # verify_brief_products.py writes fixes back to the source directory.
        rc, out, err = run_script("verify_brief_products.py",
                                  [site_slug, "--fix"], env, timeout=60)
        if rc == 0:
            report("✅ Product codes verified — all clean")
        elif rc == 2:
            report("🔧 Product codes auto-corrected — briefs updated")
            # Re-run filter_briefs.py to regenerate the filtered file from
            # the now-fixed source briefs
            rc2, out2, err2 = run_script("filter_briefs.py", [site_slug], env, timeout=60)
            if rc2 == 0:
                try:
                    with open(filtered_path) as f:
                        filtered_data = json.load(f)
                    briefs = filtered_data.get("briefs", [])
                    report(f"✅ Reloaded {len(briefs)} briefs from refreshed filtered file")
                except (json.JSONDecodeError, FileNotFoundError) as e:
                    report(f"⚠️ Failed to reload filtered briefs after fix: {e}")
            else:
                report(f"⚠️ Filter re-run failed (exit {rc2}) — using possibly stale briefs")
    else:
        report("⚠️ verify_brief_products.py not found — skipping product verification")

    # ── Phase 1.6: Skip single-product review briefs ──
    report("\n## Phase 1.6 — Product Count Gate")
    # Tour review pages need ≥2 product cards. Skip briefs that can't satisfy this.
    skipped_reviews = []
    try:
        # Load content bank to check available products
        cb_path = Path(os.path.expanduser("~/.hermes/affiliate-crons/content-banks")) / f"{site_slug}.yaml"
        if cb_path.exists():
            cb = yaml.safe_load(open(cb_path))
            all_products = cb.get("products", [])
            for brief in briefs[:]:
                if brief.get("template") == "tour_review":
                    # Check how many products are available for this topic
                    topic = brief.get("topic", "").lower()
                    dest = brief.get("destination", "").lower()
                    # Count products matching topic or destination
                    matching = [p for p in all_products if 
                        dest in str(p.get("destination", "")).lower() or
                        topic in str(p.get("title", "")).lower()]
                    if len(matching) < 2:
                        skipped_reviews.append(brief.get("slug", "unknown"))
                        briefs.remove(brief)
        if skipped_reviews:
            report(f"  ⚠️ Skipped {len(skipped_reviews)} tour_review brief(s) — content bank has <2 matching products")
            for s in skipped_reviews:
                report(f"    • {s}")
        else:
            report("  ✅ All briefs pass product count gate")
    except Exception as e:
        report(f"  ⚠️ Product count gate skipped: {e}")

    # ── Phase 1.7: Brief Replenishment — auto-heal comparison exhaustion ──
    report("\n## Phase 1.7 — Brief Replenishment")
    try:
        cb_path = Path(os.path.expanduser("~/.hermes/affiliate-crons/content-banks")) / f"{site_slug}.yaml"
        briefs_path = BRIEFS_DIR / f"{site_slug}.json"
        gen_state_path = Path(os.path.expanduser("~/.hermes/affiliate-crons/state")) / "generation_state.json"
        
        if cb_path.exists() and briefs_path.exists():
            cb = yaml.safe_load(open(cb_path))
            briefs_data = json.load(open(briefs_path))
            
            # Count available comparison briefs in current filtered pool
            remaining_comp = [b for b in briefs if b.get("template") == "comparison"]
            comp_count = len(remaining_comp)
            report(f"  Comparison briefs available: {comp_count}")
            
            if comp_count < 3:
                # Compute already-used pairings — from briefs file + generation state
                used_slugs = set()
                for b in briefs_data.get("briefs", []):
                    used_slugs.add(b.get("slug", ""))
                
                if gen_state_path.exists():
                    gen_state = json.load(open(gen_state_path))
                    used_slugs.update(gen_state.get(site_slug, {}).get("generated", []))
                
                # Get product codes and titles from content bank
                products = cb.get("products", [])
                product_map = {}
                for p in products:
                    code = p.get("viator_id") or p.get("code", "")
                    title = p.get("title", "")[:80]
                    if code:
                        product_map[code] = title
                
                # Find untapped pairings (combinations not yet in used_slugs)
                codes = list(product_map.keys())
                untapped = []
                for c1, c2 in itertools.combinations(codes, 2):
                    # Check if a comparison slug with these codes exists
                    # Normalize underscores to match slug generation
                    norm_c1 = c1.lower().replace('_', '-')
                    norm_c2 = c2.lower().replace('_', '-')
                    slug_patterns = [
                        f"{norm_c1}-vs-{norm_c2}",
                        f"{norm_c2}-vs-{norm_c1}",
                    ]
                    if not any(s in used_slugs for s in slug_patterns):
                        untapped.append((c1, c2))
                
                untapped = untapped[:10]  # limit to avoid overwhelming
                
                if untapped:
                    new_briefs = []
                    for c1, c2 in untapped[:5]:
                        t1 = product_map.get(c1, c1)[:60]
                        t2 = product_map.get(c2, c2)[:60]
                        # Create clean slug from product codes
                        clean_c1 = c1.lower().replace('_', '-')
                        clean_c2 = c2.lower().replace('_', '-')
                        slug = f"{clean_c1}-vs-{clean_c2}"
                        # Truncate slug to <100 chars
                        if len(slug) > 90:
                            slug = slug[:90].rstrip('-')
                        brief = {
                            "slug": slug,
                            "title": f"{t1} vs {t2}",
                            "template": "comparison",
                            "intent_type": "transactional",
                            "products_to_feature": [c1, c2],
                            "narrative_angle": f"Comparing two {site_slug.replace('-', ' ')} experiences for different traveler types",
                            "topic": "comparison",
                            "generated_date": datetime.now().strftime("%Y-%m-%d"),
                        }
                        new_briefs.append(brief)
                    
                    # Append to briefs data
                    existing = briefs_data.get("briefs", [])
                    existing.extend(new_briefs)
                    briefs_data["briefs"] = existing
                    # Atomic write: temp file + rename
                    import tempfile
                    fd, tmp_path = tempfile.mkstemp(suffix='.json', dir=str(briefs_path.parent))
                    with os.fdopen(fd, 'w') as f:
                        json.dump(briefs_data, f, indent=2)
                    os.replace(tmp_path, briefs_path)  # atomic on Unix
                    
                    # Re-run filter_briefs to get updated pool
                    rc2, out2, err2 = run_script("filter_briefs.py", [site_slug], env, timeout=60)
                    if rc2 == 0:
                        try:
                            with open(filtered_path) as f:
                                filtered_data = json.load(f)
                            briefs = filtered_data.get("briefs", [])
                            remaining_comp = [b for b in briefs if b.get("template") == "comparison"]
                            report(f"  🔄 Generated {len(new_briefs)} comparison briefs — pool now {len(remaining_comp)}")
                            for nb in new_briefs:
                                report(f"    • {nb['slug']}")
                        except (json.JSONDecodeError, FileNotFoundError):
                            report("  ⚠️ Briefs regenerated but filter re-run failed")
                    else:
                        report(f"  ⚠️ Briefs regenerated ({len(new_briefs)}) but filter failed (exit {rc2})")
                else:
                    report("  ⚠️ No untapped pairings — content bank may need new products")
                
                # Fallback: if comparison pool STILL < 3, allow 1 tour_review
                remaining_comp_now = [b for b in briefs if b.get("template") == "comparison"]
                if len(remaining_comp_now) < 3:
                    tour_reviews = [b for b in briefs if b.get("template") == "tour_review"]
                    if tour_reviews:
                        # Keep 1 tour_review, track the rest to skip — do NOT mutate briefs (shifts indices)
                        tour_review_slugs_allowed = {tour_reviews[0].get("slug", "")} if tour_reviews else set()
                        report(f"  🔽 Comparison pool low ({len(remaining_comp_now)}) — allowing 1 tour_review fallback")
                        report(f"    • {tour_reviews[0].get('slug', '?')}")
                    else:
                        tour_review_slugs_allowed = set()
                else:
                    tour_review_slugs_allowed = set()  # None limited — all tour_reviews pass
            else:
                report("  ✅ Comparison pool healthy")
    except Exception as e:
        report(f"  ⚠️ Brief replenishment skipped: {e}")
        import traceback
        report(f"    {traceback.format_exc()[:300]}")

    # ── Phase 2: Dedup check ──
    report("\n## Phase 2 — Dedup Check")
    rc, out, err = run_script("dedup_check.py", [site_slug], env, timeout=30)

    # Parse duplicates from output
    duplicates = set()
    for line in (out + "\n" + err).split("\n"):
        if "DUPLICATE:" in line:
            m = re.search(r"DUPLICATE:\s+([^\s—]+)", line)
            if m:
                duplicates.add(m.group(1))

    if duplicates:
        report(f"⚠️ {len(duplicates)} duplicates found — will be skipped")

    # ── Phase 3: Check state for already-generated slugs ──
    state = load_state()
    site_state = state.get(site_slug, {}).get("generated", [])

    # Filter briefs: skip duplicates and already-generated
    # Also limit tour_reviews if comparison pool is low (only 1 allowed)
    candidate_briefs = []
    tour_review_count = 0
    for b in briefs:
        slug = b.get("slug", "")
        if slug in duplicates:
            continue
        if slug in site_state:
            # Verify file actually exists on disk using language-aware path
            lang = b.get("language", "en")
            site_dir = Path(site["local_path"])
            if lang in ("de", "es"):
                slug_exists = (site_dir / lang / slug / "index.html").exists()
            else:
                slug_exists = (site_dir / slug).exists() or (site_dir / f"{slug}.html").exists()
            if slug_exists:
                continue
            else:
                report(f"🔧 Orphan detected: '{slug}' in state but no file on disk — regenerating")
        
        # Tour review limit: if comparison pool was low, only 1 tour_review allowed
        if b.get("template") == "tour_review":
            if tour_review_slugs_allowed is not None and slug not in tour_review_slugs_allowed:
                report(f"  ⏭️ Skipping tour_review (limit): {slug}")
                continue
            tour_review_count += 1
        
        candidate_briefs.append(b)

    if not candidate_briefs:
        report("ℹ️ All briefs already generated or duplicates — nothing to do")
        return

    report(f"Generating {len(candidate_briefs)} new page(s)")

    # ── Phase 3.5: Fetch product images ──
    report("\n## Phase 3.5 — Fetch Product Images")
    all_codes = set()
    for b in candidate_briefs:
        for code in b.get("products_to_feature", []):
            if code:
                all_codes.add(code)
        pc = b.get("primary_product_code", "")
        if pc:
            all_codes.add(pc)
        for code in b.get("comparison_products", []):
            if code:
                all_codes.add(code)
    if all_codes:
        rc, out, err = run_script("fetch_product_images.py",
                                  ["--fetch"], env, timeout=120)
        report(f"  Fetched images for {len(all_codes)} product codes (exit {rc})")
    else:
        report("  ℹ️ No product codes to fetch")

    # ── Phase 3.6: Visual Strategy ──
    report("\n## Phase 3.6 — Visual Strategy (pre-selecting images before generation)")
    rc, out, err = run_script("visual_strategy.py", [site_slug], env, timeout=30)
    if rc == 0:
        report(f"  ✅ Visual strategy planned")
    else:
        report(f"  ⚠️ Visual strategy skipped (exit {rc}) — injector will use heuristics")

    # ── Phase 4: Generate pages ──
    report("\n## Phase 4 — Generate Pages")
    generated = []
    failed = []

    for i, brief in enumerate(candidate_briefs):
        slug = brief.get("slug", f"brief-{i}")
        # Use brief index in the original filtered briefs array
        brief_idx = briefs.index(brief)
        report(f"  Generating: {slug} ...")

        rc, out, err = run_script("page_generator.py",
                                  [site_slug, str(brief_idx), "--briefs", filtered_path],
                                  env, timeout=300)

        if rc != 0:
            report(f"  ❌ Failed: {slug} (exit {rc})")
            if err:
                report(f"  ```\n{err[:400]}\n```")
            if out:
                report(f"  stdout: {out[:400]}")
            failed.append(slug)
            continue

        # Check if file was actually created — generator writes to GENERATED_DIR/{site_slug}/{slug}.html
        site_slug = site["site_id"]  # load_site returns site_id, not slug
        gen_file = GENERATED_DIR / site_slug / f"{slug}.html"
        if not gen_file.exists():
            # Fallback: older generator versions wrote flat
            gen_file = GENERATED_DIR / f"{slug}.html"
        if gen_file.exists():
            generated.append(slug)
            report(f"  ✅ {slug}")
        else:
            # Try site directory — generator may output directly
            site_dir = Path(site["local_path"])
            if (site_dir / f"{slug}.html").exists() or (site_dir / slug / "index.html").exists():
                generated.append(slug)
                report(f"  ✅ {slug} (direct to site)")
            else:
                report(f"  ❌ {slug} — generator ran but no output file found")
                failed.append(slug)

    if not generated:
        report("\n❌ No pages generated successfully")
        sys.exit(1)

    report(f"\n✅ Generated {len(generated)} pages, {len(failed)} failed")

    # ── Phase 4.2: QA Gate (self-healing — retries with feedback) ──
    report("\n## Phase 4.2 — QA Gate")
    qa_failures = []
    MAX_QA_RETRIES = 3

    for slug in list(generated):  # iterate copy — generated shrinks on final failure
        passed = False
        for attempt in range(1, MAX_QA_RETRIES + 1):
            rc, out, err = run_script("qa_pipeline.py", [site_slug, slug], env, timeout=120)

            if rc != 0:
                report(f"  ⚠️ QA attempt {attempt}/{MAX_QA_RETRIES} for {slug}: error (exit {rc})")
            elif "APPROVED" in out:
                report(f"  ✅ QA PASS {slug} (attempt {attempt})")
                passed = True
                break
            elif "REGENERATE" in out or "DISCARD" in out:
                if attempt < MAX_QA_RETRIES:
                    report(f"  🔄 Regenerating {slug} with QA feedback (attempt {attempt})")
                    # Look up brief for this slug from candidate_briefs
                    brief_for_slug = next((b for b in candidate_briefs if b.get("slug") == slug), None)
                    if brief_for_slug:
                        brief_idx = briefs.index(brief_for_slug)
                    else:
                        brief_idx = 0
                    rc2, out2, err2 = run_script("page_generator.py",
                                                  [site_slug, str(brief_idx), "--briefs", filtered_path,
                                                   "--feedback", out[:1000]],
                                                  env, timeout=300)
                    if rc2 != 0:
                        report(f"  ❌ Regeneration failed for {slug} (exit {rc2})")
                        if err2:
                            report(f"  ```\n{err2[:400]}\n```")
                        # Fall through to next attempt
                    else:
                        # Check if new file produced
                        gen_file = GENERATED_DIR / site_slug / f"{slug}.html"
                        if gen_file.exists() and gen_file.stat().st_size > 500:
                            continue  # try QA again
            else:
                # SKIP — article not found, already reviewed, or other non-error
                report(f"  ⏭️ QA SKIP {slug} (already reviewed)")
                passed = True
                break

        if not passed:
            report(f"  ❌ QA BLOCKED {slug} after {MAX_QA_RETRIES} attempts — discarded")
            qa_failures.append(slug)

    if qa_failures:
        report(f"\n❌ QA gate blocked {len(qa_failures)} pages: {', '.join(qa_failures)}")
        report("These pages will NOT be deployed. Check generator before next drip.")
        generated = [s for s in generated if s not in qa_failures]
        for slug in qa_failures:
            gen_file = GENERATED_DIR / site_slug / f"{slug}.html"
            if gen_file.exists():
                gen_file.unlink()
        if not generated:
            report("\nNo pages passed QA. Aborting deploy.")
            sys.exit(1)

    # ── Phase 4.5: Image pipeline ──
    report("\n## Phase 4.5 — Image Pipeline")
    for slug in generated:
        gen_file = GENERATED_DIR / site_slug / f"{slug}.html"
        if not gen_file.exists():
            gen_file = GENERATED_DIR / f"{slug}.html"
        if not gen_file.exists():
            continue
        rc, out, err = run_script("inject_page_images.py",
                                  [str(gen_file), "--fix"], env, timeout=60)
        if rc == 0:
            report(f"  🖼️ {slug} — images injected")
        else:
            report(f"  ⚠️ {slug} — image injection skipped (no eligible products)")

    # ── Phase 5: Copy to site + deploy ──
    report("\n## Phase 5 — Deploy")
    site_dir = Path(site["local_path"])
    deployed = []

    # Build language → slug mapping from candidate briefs
    lang_map = {}
    for b in candidate_briefs:
        lang_map[b.get("slug", "")] = b.get("language", "en")

    def get_deploy_path(site_dir, slug):
        """Return (dest_path, url_prefix) based on language."""
        lang = lang_map.get(slug, "en")
        if lang in ("de", "es"):
            lang_dir = site_dir / lang / slug
            lang_dir.mkdir(parents=True, exist_ok=True)
            return (lang_dir / "index.html", f"/{lang}/{slug}/")
        return (site_dir / f"{slug}.html", f"/{slug}")

    for slug in generated:
        # Generator writes to GENERATED_DIR/{site_slug}/{slug}.html
        gen_file = GENERATED_DIR / site_slug / f"{slug}.html"
        if not gen_file.exists():
            # Fallback: older generator versions wrote flat
            gen_file = GENERATED_DIR / f"{slug}.html"
        if not gen_file.exists():
            # Check if already in site dir
            dest_path, _ = get_deploy_path(site_dir, slug)
            if dest_path.exists():
                deployed.append(slug)
                continue
            report(f"  ⚠️ {slug} — no file to deploy")
            continue

        # Copy to site
        dest_path, url_prefix = get_deploy_path(site_dir, slug)
        shutil.copy2(str(gen_file), str(dest_path))
        deployed.append(slug)
        report(f"  📄 {slug} → {site['domain']}{url_prefix}")

    if not deployed:
        report("⚠️ No pages to deploy")
        return

    # ── Phase 5.5: Dead-Link Strip ──
    report("\n## Phase 5.5 — Dead-Link Strip")
    try:
        rc, out, err = run_script("strip_dead_links.py",
                                  [str(site_dir), "--fix"], env, timeout=30)
        if rc == 0:
            report("  ✅ Dead-link check complete")
        else:
            report(f"  ⚠️ Dead-link strip issue (exit {rc})")
    except Exception as e:
        report(f"  ⚠️ Dead-link strip skipped: {e}")

    # ── Phase 5.6: Site Preflight ──
    report("\n## Phase 5.6 — Site Preflight")
    preflight_issues = []
    for slug in deployed:
        # Use language-aware path
        lang = lang_map.get(slug, "en")
        if lang in ("de", "es"):
            page_path = site_dir / lang / slug / "index.html"
        else:
            page_path = site_dir / f"{slug}.html"
        if not page_path.exists():
            continue
        content = page_path.read_text()

        # Stock image check
        if 'stock-' in content:
            import re as _re
            matches = _re.findall(r'stock-[^\"]+', content)
            if matches:
                preflight_issues.append(f"stock image: {matches[0]} in {slug}")
                continue

        # Brand leak check — wrong site names
        # (Skip for now — requires per-site brand name config)

        # OG tag check
        if 'property="og:title"' not in content:
            preflight_issues.append(f"missing og:title in {slug}")

        # Affiliate link check — viator.com without medium=link
        if 'viator.com' in content and 'medium=link' not in content:
            preflight_issues.append(f"viator link missing medium=link in {slug}")

        # Garbled fragment check
        if 'for extended s.' in content.lower() or 'for an extended s.' in content.lower():
            preflight_issues.append(f"garbled fragment in {slug}")

        # Unrendered template variable check
        if '{DOMAIN}' in content or '{{DOMAIN}}' in content:
            preflight_issues.append(f"unrendered template variable in {slug}")

    if preflight_issues:
        report(f"⚠️ {len(preflight_issues)} preflight issue(s):")
        for issue in preflight_issues:
            report(f"  - {issue}")
    else:
        report(f"✅ All {len(deployed)} pages pass preflight")

    # ── Phase 5.8: Inline Link Injection ──
    # Inject inline Viator affiliate links on newly generated pages.
    # Runs after preflight (content is verified) but before sitemap (links must be in final HTML).
    report("\n## Phase 5.8 — Inline Link Injection")
    injected = 0
    for slug in deployed:
        lang = lang_map.get(slug, "en")
        if lang in ("de", "es"):
            page_path = site_dir / lang / slug / "index.html"
        else:
            page_path = site_dir / f"{slug}.html"
        if not page_path.exists():
            continue
        try:
            rc, out, err = run_script("inject_all_inline_links.py",
                                      [str(page_path), "--fix"], env, timeout=30)
            if rc == 0 and out:
                # Extract link count from output: "Links injected: N"
                import re as _re2
                m = _re2.search(r'Links injected: (\d+)', out)
                if m and int(m.group(1)) > 0:
                    injected += int(m.group(1))
        except Exception:
            pass  # Non-critical — page deploys without inline links
    if injected:
        report(f"  ✅ Injected {injected} inline Viator links across {len(deployed)} page(s)")
    else:
        report(f"  ℹ️ No inline links injected (no eligible strong-text phrases)")

    # ── Phase 5.7: Sitemap regen ──
    report("\n## Phase 5.7 — Sitemap Regen")
    try:
        rc, out, err = run_script("sitemap-generator.py",
                                  ["--dir", str(site_dir), "--domain", site['domain']],
                                  env, timeout=30)
        if rc == 0:
            report("  ✅ Sitemap regenerated")
        else:
            report(f"  ❌ Sitemap regen FAILED (exit {rc}) — aborting deploy. Fix before next drip.")
            if err:
                report(f"  ```\n{err[:500]}\n```")
            sys.exit(1)
    except Exception as e:
        report(f"  ⚠️ Sitemap regen skipped: {e}")

    # ── Git commit + push ──
    report("\n## Git")
    try:
        subprocess.run(["git", "add", "."], cwd=str(site_dir),
                       capture_output=True, text=True, env=env, timeout=15)
        subprocess.run(["git", "commit", "-m",
                        f"content drip: {', '.join(deployed)}"],
                       cwd=str(site_dir), capture_output=True, text=True, env=env, timeout=15)
        result = subprocess.run(["git", "push", "origin", "main"],
                                cwd=str(site_dir), capture_output=True, text=True,
                                env=env, timeout=60)
        if result.returncode == 0:
            report("✅ Pushed to origin/main")
            # ── GSC Sitemap Submit (Phase 3: submit updated sitemap to GSC) ──
            report("\n## GSC Sitemap Submit")
            rc, out, err = run_script("gsc_sitemap_submit.py", [site_slug, "--no-regen"], env, timeout=60)
            if rc == 0:
                report("  ✅ Sitemap submitted to GSC")
            else:
                report(f"  ⚠️ Sitemap submit issue (exit {rc}): {err[:200]}")
        else:
            report(f"⚠️ Push issue: {result.stderr[:200]}")
    except Exception as e:
        report(f"⚠️ Git error: {e}")

    # ── Phase 6: Update state ──
    report("\n## State Update")
    if site_slug not in state:
        state[site_slug] = {}
    if "generated" not in state[site_slug]:
        state[site_slug]["generated"] = []
    for slug in deployed:
        if slug not in state[site_slug]["generated"]:
            state[site_slug]["generated"].append(slug)
    save_state(state)
    report(f"✅ Updated generation_state.json — {len(deployed)} new slugs")

    # ── Summary ──
    report(f"\n---\n**Done:** {len(deployed)} deployed to {site['domain']}")
    report(f"**URLs:**")
    for slug in deployed:
        lang = lang_map.get(slug, "en")
        if lang in ("de", "es"):
            report(f"- https://www.{site['domain']}/{lang}/{slug}")
        else:
            report(f"- https://www.{site['domain']}/{slug}")
    if failed:
        report(f"\n**Failed:** {', '.join(failed)}")


if __name__ == "__main__":
    main()
