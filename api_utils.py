"""
Shared API utilities for the affiliate content pipeline.
Retry with exponential backoff, safe API calls, structured logging.
"""
import time, json
from urllib.request import Request, urlopen

def call_with_retry(url, payload=None, headers=None, method="POST",
                    max_retries=3, base_delay=2, timeout=120):
    """
    Call an API endpoint with exponential backoff.
    Returns (response_data, None) on success, (None, error_message) on failure.
    Handles: 429 (rate limit), 503 (server error), transient network errors.
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            data_bytes = json.dumps(payload).encode("utf-8") if payload else None
            req = Request(url, data=data_bytes, method=method)
            req.add_header("Content-Type", "application/json")

            if headers:
                for k, v in headers.items():
                    req.add_header(k, v)

            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status

            if status == 429:
                retry_after = resp.headers.get("Retry-After", base_delay * (2 ** attempt))
                wait = int(retry_after) if retry_after.isdigit() else base_delay * (2 ** attempt)
                if attempt < max_retries:
                    time.sleep(wait)
                    continue
                return None, f"Rate limited after {max_retries + 1} attempts"

            return json.loads(raw), None

        except Exception as e:
            last_error = str(e)
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                continue

    return None, f"Failed after {max_retries + 1} attempts: {last_error}"
