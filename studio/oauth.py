"""Renew short-lived social tokens without writing secrets to the repository."""
from __future__ import annotations

import os
import time

from . import http as requests


def ensure_tiktok_access(store) -> str:
    """Return a usable TikTok access token and persist rotated tokens encrypted.

    TikTok access tokens are short lived. The first direct-mode run seeds from
    GitHub Secrets, refreshes when possible, then keeps the rotated access and
    refresh tokens inside the encrypted studio state. Nothing is printed.
    """
    mode = os.getenv("STUDIO_PUBLISHER", "review")
    targets = {x.strip() for x in os.getenv("STUDIO_TARGETS", "").split(",") if x.strip()}
    if mode != "direct" or "tiktok" not in targets:
        return ""

    oauth = store.data.setdefault("oauth", {}).setdefault("tiktok", {})
    access = oauth.get("access_token") or os.getenv("TIKTOK_ACCESS_TOKEN", "")
    refresh = oauth.get("refresh_token") or os.getenv("TIKTOK_REFRESH_TOKEN", "")
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "")
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "")
    expires_at = float(oauth.get("expires_at") or 0)
    now = time.time()

    # If refresh credentials are available, refresh on first managed run and
    # again ten minutes before expiry. A rotated refresh token is checkpointed.
    should_refresh = bool(refresh and client_key and client_secret) and (
        not oauth.get("access_token") or expires_at <= now + 600)
    if should_refresh:
        r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }, timeout=45)
        if not r.ok:
            raise RuntimeError(f"TikTok token refresh failed: HTTP {r.status_code}")
        obj = r.json()
        new_access = obj.get("access_token")
        new_refresh = obj.get("refresh_token")
        if not new_access or not new_refresh:
            raise RuntimeError("TikTok token refresh returned incomplete credentials")
        oauth.update({
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_at": now + int(obj.get("expires_in") or 86400),
            "refresh_expires_at": now + int(obj.get("refresh_expires_in") or 31536000),
            "scope": obj.get("scope", ""),
            "token_type": obj.get("token_type", "Bearer"),
        })
        # Durable encrypted checkpoint before the new token is used remotely.
        store.save()
        access = new_access

    if not access:
        raise RuntimeError("TikTok direct mode needs an access token or refresh-token credentials")
    os.environ["TIKTOK_ACCESS_TOKEN"] = access
    return access
