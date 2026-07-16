#!/bin/bash
# deploy.sh — Gate-Protected Deploy (Preview → Promote Flow)
# The ONLY documented path to production.
#
# Usage: deploy.sh <site_slug>
#
# Flow:
#   1. Gate self-tests (verify gates aren't broken)
#   2. Full gate suite (build_check.sh all)
#   3. Commit changes to git
#   4. vercel deploy → PREVIEW URL (not production)
#   5. Smoke test against preview
#   6. IF ALL PASS: vercel promote → production
#   7. Write deploy ledger
#   8. IF ANY FAIL: DO NOT promote. Alert. Site stays on last-good deploy.
#
# Requires: Vercel CLI token in ~/.vercel/auth.json

set -e

SITE_SLUG="${1:-}"
if [ -z "$SITE_SLUG" ]; then
    echo "ERROR: Site slug required"
    echo "Usage: deploy.sh <site_slug>"
    exit 1
fi

SITE_DIR="$HOME/sites/$SITE_SLUG"
BUILD_CHECK="$HOME/.hermes/scripts/build_check.sh"
LEDGER="$HOME/.hermes/affiliate-crons/state/deploy_ledger.json"
DOMAIN="www.${SITE_SLUG}.com"

if [ ! -d "$SITE_DIR" ]; then
    echo "ERROR: Site directory not found: $SITE_DIR"
    exit 1
fi

cd "$SITE_DIR"

echo ""
echo "═══════════════════════════════════════════════"
echo "  DEPLOY — $SITE_SLUG"
echo "  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "═══════════════════════════════════════════════"
echo ""

# ── Step 0: Gate Self-Tests ──────────────────────────────────
echo "[STEP 0] Gate self-tests..."
if ! bash "$BUILD_CHECK" --self-test; then
    echo ""
    echo "❌ DEPLOY BLOCKED — gate self-tests failed."
    echo "   Fix the broken gates before deploying."
    exit 1
fi
echo ""

# ── Step 1: Full Gate Suite ──────────────────────────────────
echo "[STEP 1] Full gate suite..."
if ! bash "$BUILD_CHECK" "$SITE_DIR" all; then
    echo ""
    echo "❌ DEPLOY BLOCKED — quality gates failed."
    echo "   Fix the issues above and re-run deploy.sh"
    exit 1
fi
echo ""

# ── Step 2: Commit Changes ───────────────────────────────────
echo "[STEP 2] Commit changes..."
SHA=$(git rev-parse HEAD 2>/dev/null || echo "no-git")
if git diff --quiet && git diff --cached --quiet; then
    echo "  Working tree clean — no changes to commit"
else
    git add -A
    COMMIT_MSG="deploy: $(date -u +%Y-%m-%dT%H:%M:%SZ) — gates passed"
    git commit -m "$COMMIT_MSG" --allow-empty
    SHA=$(git rev-parse HEAD)
    echo "  Committed: $SHA"
fi
echo ""

# ── Step 3: Deploy to Preview ────────────────────────────────
echo "[STEP 3] Deploying to preview URL..."
DEPLOY_OUTPUT=$(vercel deploy --yes 2>&1)
PREVIEW_URL=$(echo "$DEPLOY_OUTPUT" | grep -o 'https://[a-zA-Z0-9._-]*\.vercel\.app' | head -1)

if [ -z "$PREVIEW_URL" ]; then
    echo "❌ Failed to extract preview URL from deploy output:"
    echo "$DEPLOY_OUTPUT"
    exit 2
fi
echo "  Preview: $PREVIEW_URL"
echo ""

# ── Step 4: Smoke Test Against Preview ───────────────────────
echo "[STEP 4] Smoke test against preview..."
SMOKE_FAIL=0

# Check homepage
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$PREVIEW_URL/" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
    echo "  ❌ Homepage: $HTTP_CODE (expected 200)"
    SMOKE_FAIL=1
else
    echo "  ✅ Homepage: 200"
fi

# Check sitemap
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$PREVIEW_URL/sitemap.xml" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
    echo "  ❌ Sitemap: $HTTP_CODE (expected 200)"
    SMOKE_FAIL=1
else
    echo "  ✅ Sitemap: 200"
fi

# Check CSS
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$PREVIEW_URL/css/style.css" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
    echo "  ❌ CSS: $HTTP_CODE (expected 200)"
    SMOKE_FAIL=1
else
    echo "  ✅ CSS: 200"
fi

if [ "$SMOKE_FAIL" -ne 0 ]; then
    echo ""
    echo "❌ SMOKE TEST FAILED — preview is broken."
    echo "   Site was NOT promoted to production."
    echo "   Preview URL for debugging: $PREVIEW_URL"
    exit 2
fi
echo ""

# ── Step 5: Promote to Production ────────────────────────────
echo "[STEP 5] Promoting to production..."
vercel promote --yes 2>&1
PROMOTE_EXIT=$?

if [ "$PROMOTE_EXIT" -ne 0 ]; then
    echo "❌ Promote failed."
    exit 2
fi
echo ""

# ── Step 6: Production Verification ──────────────────────────
echo "[STEP 6] Production verification..."
sleep 5  # Allow alias propagation

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN/" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    echo "  ✅ Production: https://$DOMAIN/ → 200"
else
    echo "  ⚠️  Production: $HTTP_CODE (may still be propagating)"
fi
echo ""

# ── Step 7: Write Deploy Ledger ──────────────────────────────
echo "[STEP 7] Writing deploy ledger..."
DEPLOY_ID=$(vercel inspect "$PREVIEW_URL" 2>/dev/null | grep -o 'prj_[a-zA-Z0-9]*' | head -1 || echo "unknown")

mkdir -p "$(dirname "$LEDGER")"

python3 -c "
import json, os
from datetime import datetime, timezone

ledger_path = '$LEDGER'
entry = {
    'site': '$SITE_SLUG',
    'deploy_id': '$DEPLOY_ID',
    'preview_url': '$PREVIEW_URL',
    'production_url': 'https://$DOMAIN',
    'sha': '$SHA',
    'timestamp': datetime.now(timezone.utc).isoformat(),
    'gate_results': 'PASS'
}

ledger = []
if os.path.exists(ledger_path):
    with open(ledger_path) as f:
        ledger = json.load(f)

ledger.insert(0, entry)

# Keep last 50 entries
ledger = ledger[:50]

with open(ledger_path, 'w') as f:
    json.dump(ledger, f, indent=2, default=str)

print(f'  ✅ Ledger updated: {len(ledger)} entries')
"

echo ""
echo "═══════════════════════════════════════════════"
echo "  ✅ DEPLOY COMPLETE"
echo "  Site: https://$DOMAIN"
echo "  SHA:  $SHA"
echo "  Time: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "═══════════════════════════════════════════════"
