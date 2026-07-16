"""
audit_action — package backing the audit-action-loop script.

Layout:
    url_utils        URL <-> site mapping, URL file loading, local-file resolution
    audit_runner     Wraps the image-placement audit CLI
    image_injection  Viator product-code extraction and HTML <img> injection
    queue_io         Append-only queue files (kept here to avoid sys.path churn)

The orchestrator (audit-action-loop.py) imports from these modules and re-exports
the public names so existing test imports stay unchanged.
"""
