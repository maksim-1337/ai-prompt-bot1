"""One-time OAuth on the owner's computer; prints no credentials."""
import argparse
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

parser = argparse.ArgumentParser()
parser.add_argument("client_secrets", help="Downloaded Google Desktop OAuth client JSON")
args = parser.parse_args()
flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets,
            ["https://www.googleapis.com/auth/youtube.upload"])
credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
path = Path("youtube-token.json")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(credentials.to_json())
print("Saved youtube-token.json. Add its contents to YOUTUBE_TOKEN_JSON in GitHub Secrets.")
