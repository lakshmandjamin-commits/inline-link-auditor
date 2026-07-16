#!/usr/bin/env python3
"""
Exchange GSC service account JSON for an access token.
Mirrors Hanuman's pattern — outputs GSC_ACCESS_TOKEN for the CLI.

Usage:
  eval $(python3 gsc_sa_token.py)                # Export to env
  python3 gsc_sa_token.py --token-only            # Print raw token
"""
import sys, os, argparse
from google.oauth2 import service_account
from google.auth.transport.requests import Request

CRED_PATH = os.path.expanduser("~/.hermes/credentials/saraswati-gsc.json")
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

def get_token():
    creds = service_account.Credentials.from_service_account_file(
        CRED_PATH, scopes=SCOPES
    )
    creds.refresh(Request())
    return creds.token

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-only", action="store_true")
    args = parser.parse_args()
    
    token = get_token()
    if args.token_only:
        print(token)
    else:
        print(f"export GSC_ACCESS_TOKEN={token}")
