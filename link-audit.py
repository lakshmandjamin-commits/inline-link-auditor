#!/usr/bin/env python3
"""Wrapper — delegates to inline_link_audit.py (live HTML, no stale cache)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inline_link_audit import main as _audit_main

if __name__ == '__main__':
    _audit_main()
