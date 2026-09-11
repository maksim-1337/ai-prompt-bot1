"""Real social API adapters. Missing credentials never count as success."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import http as requests


class PreflightError(RuntimeError):
    """Failure known to happen before a remote publish side effect."""


class UnknownPublishState(RuntimeError):
    """A write may have succeeded, so automatic retry would risk a duplicate."""


class Ayrshare:
    base = "https://api.ayrshare.com/api"

    def __init__(self, key=None):
        key = key or os.getenv("AYRSHARE_API_KEY", "")
        if not key:
            raise PreflightError("AYRSHARE_API_KEY is not configured")
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
            raise ValueError("Supported Ayrshare targets: youtube, instagram, tiktok")
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
        raw = os.getenv("YOUTUBE_TOKEN_JSON", "")
        if not raw:
            raise PreflightError("YOUTUBE_TOKEN_JSON is not configured")
        info = json.loads(raw)
        credentials = Credentials.from_authorized_user_info(
            info, ["https://www.googleapis.com/auth/youtube.upload"])
        self.api = build("youtube", "v3", credentials=credentials, cache_discovery=False)

    def publish(self, job, path, visibility):
        from googleapiclient.http import MediaFileUpload
        request = self.api.videos().insert(part="snippet,status", body={
            "snippet": {"title": job["plan"]["title"][:100].replace("<", "").replace(">", ""),
                        "description": job["plan"]["caption"], "categoryId": "25"},
            "status": {"privacyStatus": visibility, "selfDeclaredMadeForKids": False},
        }, media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True))
        response = None
        try:
            while response is None:
                _, response = request.next_chunk(num_retries=0)
        except Exception as exc:
            raise UnknownPublishState(type(exc).__name__) from None
        return {"state": "published", "id": response["id"],
                "visibility": response.get("status", {}).get("privacyStatus", visibility),
                "url": "https://youtube.com/shorts/" + response["id"]}


class TikTok:
    """Official TikTok Content Posting API using FILE_UPLOAD."""
    base = "https://open.tiktokapis.com"

    def __init__(self):
        self.token = os.getenv("TIKTOK_ACCESS_TOKEN", "")
        if not self.token:
            raise PreflightError("TIKTOK_ACCESS_TOKEN is not configured")
        self.headers = {"Authorization": "Bearer " + self.token,
                        "Content-Type": "application/json; charset=UTF-8"}

    @staticmethod
    def _ok(r, label):
        if not r.ok:
            raise RuntimeError(f"TikTok {label}: HTTP {r.status_code}")
        obj = r.json()
        if obj.get("error", {}).get("code") not in (None, "ok"):
            raise RuntimeError("TikTok " + label + ": " + str(obj.get("error", {}).get("code")))
        return obj

    def creator_info(self):
        r = requests.post(self.base + "/v2/post/publish/creator_info/query/",
                          headers=self.headers, json={}, timeout=45)
        obj = self._ok(r, "creator info")
        data = obj.get("data") or {}
        if not data.get("privacy_level_options"):
            raise RuntimeError("TikTok creator info returned no privacy options")
        return data

    def review_info(self, visibility):
        info = self.creator_info()
        requested = os.getenv("STUDIO_TIKTOK_PRIVACY",
                              "PUBLIC_TO_EVERYONE" if visibility == "public" else "SELF_ONLY")
        options = info.get("privacy_level_options", [])
        if requested not in options:
            raise PreflightError("Requested TikTok privacy is not available for this account: " + ",".join(options))
        return {"creator_username": info.get("creator_username", ""),
                "creator_nickname": info.get("creator_nickname", ""),
                "privacy": requested, "privacy_options": options,
                "max_video_post_duration_sec": info.get("max_video_post_duration_sec"),
                "disable_comment": bool(info.get("comment_disabled", False)),
                "disable_duet": bool(info.get("duet_disabled", False)),
                "disable_stitch": bool(info.get("stitch_disabled", False))}

    def publish(self, job, path: Path, visibility):
        approved = job.get("platform_meta", {}).get("tiktok") or self.review_info(visibility)
        current = self.creator_info()
        privacy = approved.get("privacy")
        if privacy not in current.get("privacy_level_options", []):
            raise PreflightError("Approved TikTok privacy is no longer available; request a new preview")
        if approved.get("creator_username") and approved.get("creator_username") != current.get("creator_username"):
            raise PreflightError("TikTok account changed after approval; request a new preview")
        for approved_key, current_key in (("disable_comment", "comment_disabled"),
                                          ("disable_duet", "duet_disabled"),
                                          ("disable_stitch", "stitch_disabled")):
            if bool(approved.get(approved_key)) != bool(current.get(current_key)):
                raise PreflightError("TikTok interaction settings changed after approval; request a new preview")
        max_duration = approved.get("max_video_post_duration_sec")
        if max_duration and float(job.get("manifest", {}).get("duration", 0)) > float(max_duration):
            raise PreflightError("Video is longer than the current TikTok account limit")
        size = path.stat().st_size
        if size <= 0 or size > 64 * 1024 * 1024:
            raise PreflightError("TikTok direct uploader expects a video between 1 byte and 64 MB")
        post_info = {"title": job["plan"]["caption"][:2200], "privacy_level": privacy,
                     "disable_comment": bool(approved.get("disable_comment")),
                     "disable_duet": bool(approved.get("disable_duet")),
                     "disable_stitch": bool(approved.get("disable_stitch")),
                     "video_cover_timestamp_ms": 1000,
                     "brand_content_toggle": False, "brand_organic_toggle": False,
                     "is_aigc": True}
        body = {"post_info": post_info,
                "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                "chunk_size": size, "total_chunk_count": 1}}
        init = self._ok(requests.post(self.base + "/v2/post/publish/video/init/",
                                     headers=self.headers, json=body, timeout=60), "publish init")
        data = init.get("data") or {}
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise RuntimeError("TikTok publish init returned no upload target")
        payload = path.read_bytes()
        try:
            upload = requests.put(upload_url,
                                  headers={"Content-Type": "video/mp4", "Content-Length": str(size),
                                           "Content-Range": f"bytes 0-{size-1}/{size}"},
                                  raw=payload, timeout=240)
        except Exception as exc:
            raise UnknownPublishState(type(exc).__name__) from None
        if upload.status_code not in (200, 201, 206):
            raise UnknownPublishState(f"TikTok upload HTTP {upload.status_code}")
        return {"state": "processing", "remote_id": publish_id}

    def status(self, publish_id):
        obj = self._ok(requests.post(self.base + "/v2/post/publish/status/fetch/",
                                    headers=self.headers, json={"publish_id": publish_id}, timeout=45),
                       "status")
        data = obj.get("data") or {}
        status = data.get("status")
        if status == "FAILED":
            return {"state": "failed", "error": data.get("fail_reason", "tiktok_failed")}
        if status == "PUBLISH_COMPLETE":
            ids = data.get("publicaly_available_post_id") or []
            return {"state": "published", "id": str(ids[0]) if ids else publish_id}
        return {"state": "processing"}


class Instagram:
    """Official Instagram Reels publishing with a resumable local upload."""
    def __init__(self):
        self.token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
        self.user_id = os.getenv("INSTAGRAM_USER_ID", "")
        self.version = os.getenv("META_API_VERSION", "v25.0")
        if not self.token or not self.user_id:
            raise PreflightError("INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID are required")
        self.graph = f"https://graph.facebook.com/{self.version}"

    def publish(self, job, path: Path, visibility):
        if visibility != "public":
            raise PreflightError("Instagram Reels direct publishing requires public visibility")
        create = requests.post(f"{self.graph}/{self.user_id}/media", data={
            "media_type": "REELS", "upload_type": "resumable",
            "caption": job["plan"]["caption"][:2200], "share_to_feed": "true",
            "access_token": self.token}, timeout=60)
        if not create.ok:
            raise PreflightError(f"Instagram container HTTP {create.status_code}")
        obj = create.json()
        container = obj.get("id")
        if not container:
            raise PreflightError("Instagram did not return a media container id")
        upload_url = obj.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.version}/{container}"
        size = path.stat().st_size
        try:
            upload = requests.post(upload_url, headers={
                "Authorization": "OAuth " + self.token, "offset": "0",
                "file_size": str(size), "Content-Type": "video/mp4",
                "Content-Length": str(size)}, raw=path.read_bytes(), timeout=240)
        except Exception as exc:
            raise UnknownPublishState(type(exc).__name__) from None
        if not upload.ok:
            raise UnknownPublishState(f"Instagram upload HTTP {upload.status_code}")
        return {"state": "processing", "phase": "uploaded", "remote_id": container}

    def status(self, container):
        r = requests.get(f"{self.graph}/{container}", params={
            "fields": "status_code,status", "access_token": self.token}, timeout=45)
        if not r.ok:
            raise RuntimeError(f"Instagram status HTTP {r.status_code}")
        data = r.json()
        status = data.get("status_code")
        if status in ("ERROR", "EXPIRED"):
            return {"state": "failed", "error": data.get("status", status)}
        if status == "PUBLISHED":
            return {"state": "published", "id": container}
        if status == "FINISHED":
            return {"state": "ready"}
        return {"state": "processing"}

    def finalize(self, container):
        try:
            final = requests.post(f"{self.graph}/{self.user_id}/media_publish", data={
                "creation_id": container, "access_token": self.token}, timeout=60)
        except Exception as exc:
            raise UnknownPublishState(type(exc).__name__) from None
        if not final.ok:
            raise UnknownPublishState(f"Instagram media_publish HTTP {final.status_code}")
        media_id = final.json().get("id")
        if not media_id:
            raise UnknownPublishState("Instagram returned no published media id")
        return {"state": "published", "id": media_id}


class Facebook:
    """Official Facebook Page Reels API with a local file upload."""
    def __init__(self):
        self.token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "")
        self.version = os.getenv("META_API_VERSION", "v25.0")
        if not self.token:
            raise PreflightError("FACEBOOK_PAGE_ACCESS_TOKEN is not configured")
        self.graph = f"https://graph.facebook.com/{self.version}"

    def publish(self, job, path: Path, visibility):
        if visibility != "public":
            raise PreflightError("Facebook Reels direct publishing currently supports public mode here")
        start = requests.post(f"{self.graph}/me/video_reels", data={
            "access_token": self.token, "upload_phase": "start"}, timeout=60)
        if not start.ok:
            raise PreflightError(f"Facebook start HTTP {start.status_code}")
        obj = start.json()
        video_id, upload_url = obj.get("video_id"), obj.get("upload_url")
        if not video_id or not upload_url:
            raise PreflightError("Facebook did not return video_id/upload_url")
        size = path.stat().st_size
        try:
            upload = requests.post(upload_url, headers={
                "Authorization": "OAuth " + self.token, "offset": "0",
                "file_size": str(size), "Content-Type": "application/octet-stream",
                "Content-Length": str(size)}, raw=path.read_bytes(), timeout=240)
            if not upload.ok:
                raise UnknownPublishState(f"Facebook upload HTTP {upload.status_code}")
            finish = requests.post(f"{self.graph}/me/video_reels", data={
                "access_token": self.token, "video_id": video_id, "upload_phase": "finish",
                "video_state": "PUBLISHED", "description": job["plan"]["caption"][:2200],
                "title": job["plan"]["title"][:255]}, timeout=60)
        except UnknownPublishState:
            raise
        except Exception as exc:
            raise UnknownPublishState(type(exc).__name__) from None
        if not finish.ok or not finish.json().get("success"):
            raise UnknownPublishState(f"Facebook finish HTTP {finish.status_code}")
        return {"state": "processing", "remote_id": str(video_id)}

    def status(self, video_id):
        r = requests.get(f"{self.graph}/{video_id}", params={
            "fields": "status", "access_token": self.token}, timeout=45)
        if not r.ok:
            raise RuntimeError(f"Facebook status HTTP {r.status_code}")
        status = (r.json().get("status") or {})
        phases = [status.get("uploading_phase", {}).get("status"),
                  status.get("processing_phase", {}).get("status"),
                  status.get("publishing_phase", {}).get("status")]
        if any(str(x).lower() in ("error", "failed") for x in phases):
            return {"state": "failed", "error": "facebook_processing_failed"}
        if str(status.get("publishing_phase", {}).get("status", "")).lower() == "complete":
            return {"state": "published", "id": str(video_id)}
        return {"state": "processing"}


def direct_provider(platform):
    factories = {"youtube": YouTube, "tiktok": TikTok, "instagram": Instagram, "facebook": Facebook}
    if platform not in factories:
        raise PreflightError("Unsupported direct target: " + platform)
    return factories[platform]()
