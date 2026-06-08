"""
auth_google.py — Run this ONCE locally to authorise Google Calendar access.
It opens a browser window, you log in, and saves a token.pickle file.
After running this, upload the token to Railway as an env var.

Usage:
    python auth_google.py
"""

import pickle
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def main():
    print("🔐 Starting Google Calendar authorisation...")
    print("A browser window will open. Log in with your Google account and click Allow.\n")

    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)

    # Save as pickle
    with open("token.pickle", "wb") as f:
        pickle.dump(creds, f)
    print("✅ token.pickle saved.\n")

    # Also save as JSON for Railway env var
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes)
    }
    with open("token.json", "w") as f:
        json.dump(token_data, f)
    print("✅ token.json saved.\n")

    print("=" * 60)
    print("NEXT STEP: Copy the contents of token.json into Railway")
    print("as an environment variable named GOOGLE_TOKEN_JSON")
    print("=" * 60)
    print("\nContents of token.json (copy ALL of this):\n")
    print(json.dumps(token_data, indent=2))


if __name__ == "__main__":
    main()
