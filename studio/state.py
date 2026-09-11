"""Encrypted queue. Every side effect is preceded by a durable intent record."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from . import http as requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes


class Store:
    def __init__(self, token: str, path="data/studio-state.enc", remote=None):
        secret = os.getenv("STUDIO_STATE_KEY") or token
        if not secret:
            raise ValueError("State encryption requires a bot token or STUDIO_STATE_KEY")
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"newslight-studio-v1",
                   info=b"encrypted-approval-queue").derive(secret.encode())
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        self.path, self.sha = Path(path), None
        self.remote = remote if remote is not None else bool(os.getenv("GITHUB_ACTIONS"))
        self.repo = os.getenv("GITHUB_REPOSITORY", "")
        self.headers = {"Authorization": "Bearer " + os.getenv("GH_TOKEN", ""),
                        "Accept": "application/vnd.github+json"}
        if self.remote and (not self.repo or not os.getenv("GH_TOKEN")):
            raise ValueError("GitHub state needs GITHUB_REPOSITORY and GH_TOKEN")
        self.url = f"https://api.github.com/repos/{self.repo}/contents/{path}"
        self.data = {"version": 1, "offset": 0, "owner": 0, "jobs": {}, "paused": False}
        blob = None
        if self.remote:
            r = requests.get(self.url, headers=self.headers, params={"ref": "main"}, timeout=30)
            if r.status_code == 200:
                obj = r.json()
                self.sha = obj["sha"]
                blob = base64.b64decode(obj["content"])
            elif r.status_code != 404:
                raise RuntimeError(f"Cannot load queue: HTTP {r.status_code}")
        elif self.path.exists():
            blob = self.path.read_bytes()
        if blob is not None:
            # Do NOT silently reset corrupt state or a queue encrypted with another key.
            self.data = json.loads(self.cipher.decrypt(blob))
            if self.data.get("version") != 1:
                raise ValueError("Unsupported queue version")

    def save(self):
        blob = self.cipher.encrypt(json.dumps(self.data, ensure_ascii=False).encode())
        if self.remote:
            payload = {"message": "Persist encrypted video queue [skip ci]", "branch": "main",
                       "content": base64.b64encode(blob).decode()}
            if self.sha:
                payload["sha"] = self.sha
            r = requests.put(self.url, json=payload, headers=self.headers, timeout=30)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Queue checkpoint failed: HTTP {r.status_code}; no further effects")
            self.sha = r.json()["content"]["sha"]
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(blob)
            tmp.chmod(0o600)
            tmp.replace(self.path)


def media_hash(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
