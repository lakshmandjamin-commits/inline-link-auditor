#!/usr/bin/env python3
"""Initialize the affiliate fleet database. Idempotent — safe to run multiple times.

Note: viator_cli.db is auto-created by viator_cli.py on first use — no need to init it here.
Only site_registry.db needs explicit initialization."""
import sqlite3, os

DB_DIR = os.path.expanduser("~/.hermes/affiliate-crons/db")
os.makedirs(DB_DIR, exist_ok=True)

# ── Site Registry DB ─────────────────────────────────────────────────────────
reg_db = sqlite3.connect(os.path.join(DB_DIR, "site_registry.db"))
reg_db.execute("""
    CREATE TABLE IF NOT EXISTS sites (
        site_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        domain TEXT NOT NULL,
        local_path TEXT NOT NULL,
        viator_pid TEXT,
        viator_mcid TEXT DEFAULT '42383',
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_validated_at TIMESTAMP,
        last_product_sync_at TIMESTAMP,
        last_price_check_at TIMESTAMP,
        last_link_check_at TIMESTAMP,
        lighthouse_perf INTEGER,
        lighthouse_a11y INTEGER,
        lighthouse_seo INTEGER,
        lighthouse_best INTEGER,
        total_pages INTEGER DEFAULT 0,
        total_products INTEGER DEFAULT 0,
        monthly_revenue_estimate REAL,
        notes TEXT
    )
""")

reg_db.execute("""
    CREATE TABLE IF NOT EXISTS site_products (
        site_id TEXT NOT NULL,
        product_code TEXT NOT NULL,
        page_url TEXT NOT NULL,
        card_position INTEGER,
        displayed_price REAL,
        last_verified TIMESTAMP,
        PRIMARY KEY (site_id, product_code, page_url),
        FOREIGN KEY (site_id) REFERENCES sites(site_id)
    )
""")

reg_db.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id TEXT,
        check_type TEXT,
        status TEXT,
        issues_found INTEGER,
        details TEXT,
        run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (site_id) REFERENCES sites(site_id)
    )
""")

reg_db.commit()
reg_db.close()

print("Database initialized: site_registry.db")
print(f"(viator_cli.db is auto-created by viator_cli.py on first use)")
print(f"Location: {DB_DIR}")
