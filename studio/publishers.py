"""Real API adapters. Missing credentials never turn into successful publication."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import http as requests


class Ayrshare:
    base = "https://api.ayrshare.com/api"

    def __init__(self, key=None):
        key = key or os.getenv("AYRSHARE_API_KEY", "")
        if not key:
            raise RuntimeError("AYRSHARE_API_KEY is not configured")
        self.headers = {"Authorization": "Bearer " + key}

    def upload(self, path: Path):
        if path.stat().st_size >= 30_000_000:
            raise ValueError("Ayrshare upload limit: 30 MB")
        with path.open("rb") as f:
            r = requests.post(self.base + "/media/upload", headers=self.headers,
                              files={"file": ("short.mp4", f, "video/mp4")},
                              data={"fileName": "short.mp4"}, timeout=180)
        if not r.ok or not r.json().get("url"):
            raise RuntimeError(f"Media upload failed: HTTP {r.status_code}")
        return r.json()["url"]

    @staticmethod
    def payload(job, platform, media_url, visibility):
        plan = job["plan"]
        body = {"post": plan["caption"], "platforms": [platform], "mediaUrls": [media_url],
                "isVideo": True, "idempotencyKey": f"nl-{job['id']}-{job['revision']}-{platform}"}
        if platform == "youtube":
            body["youTubeOptions"] = {"title": plan["title"][:100].replace("<", "").replace(">", ""),
                                     "visibility": visibility, "shorts": True, "madeForKids": False}
        elif platform == "instagram":
            body["instagramOptions"] = {"shareReelsFeed": True}
        elif platform == "tiktok":
            body["tikTokOptions"] = {"visibility": visibility, "disableComments": True,
                                     "disableDuet": True, "disableStitch": True,
                                     "isAIGenerated": True}
        else:
            raise ValueError("Supported targets: youtube, instagram, tiktok")
        return body

    def publish(self, job, platform, media_url, visibility):
        r = requests.post(self.base + "/post", headers=self.headers,
                          json=self.payload(job, platform, media_url, visibility), timeout=180)
        if not r.ok:
            raise RuntimeError(f"Publisher returned HTTP {r.status_code}; check dashboard before retry")
        obj = r.json()
        if not obj.get("id"):
            raise RuntimeError("Publisher gave no tracking ID; check dashboard before retry")
        return obj

    def status(self, remote_id):
        r = requests.get(self.base + "/post/" + remote_id, headers=self.headers, timeout=45)
        if not r.ok:
            raise RuntimeError(f"Status check failed: HTTP {r.status_code}")
        return r.json()


def platform_result(payload, platform):
    for error in payload.get("errors", []):
        if error.get("platform") in (None, platform):
            return {"state": "failed", "error": str(error.get("code", "provider_error"))}
    for item in payload.get("postIds", []):
        if item.get("platform") == platform:
            if item.get("id") == "failed" or item.get("status") == "error":
                return {"state": "failed"}
            if item.get("status") == "success" and item.get("id") not in (None, "pending"):
                return {"state": "published", "url": item.get("postUrl", ""), "id": item["id"]}
    return {"state": "processing"}


class YouTube:
    """Direct OAuth upload, without a paid cross-posting service."""
    def __init__(self):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        info = json.loads(os.environ["YOUTUBE_TOKEN_JSON"])
        credentials = Credentials.from_authorized_user_info(info, ["https://www.googleapis.com/auth/youtube.upload"])
        self.api = build("youtube", "v3", credentials=credentials, cache_discovery=False)

    def publish(self, job, path, visibility):
        from googleapiclient.http import MediaFileUpload
        request = self.api.videos().insert(part="snippet,status", body={
            "snippet": {"title": job["plan"]["title"][:100].replace("<", "").replace(">", ""),
                        "description": job["plan"]["caption"], "categoryId": "25"},
            "status": {"privacyStatus": visibility, "selfDeclaredMadeForKids": False},
        }, media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True))
        response = None
        while response is None:
            _, response = request.next_chunk(num_retries=0)
        return {"state": "uploaded", "id": response["id"],
                "visibility": response.get("status", {}).get("privacyStatus", "unknown"),
                "url": "https://youtube.com/shorts/" + response["id"]}
