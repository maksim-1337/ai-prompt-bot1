from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

START, END = "<!-- POST -->", "<!-- /POST -->"
STATUSES = {"review", "approved", "skip", "published"}


class StrictLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("Duplicate/non-string metadata key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid state: {path.name}")
    return value


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as out:
        out.write(text)
        out.flush()
        import os
        os.fsync(out.fileno())
    temp.replace(path)


def save_json(path: Path, data: dict) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def parse_draft(raw: str) -> tuple[dict, str, str]:
    if not raw.startswith("---\n") or "\n---\n" not in raw[4:]:
        raise ValueError("Missing YAML metadata")
    header, rest = raw[4:].split("\n---\n", 1)
    meta = yaml.load(header, Loader=StrictLoader)
    if not isinstance(meta, dict) or meta.get("schema_version") != 1:
        raise ValueError("Unsupported draft schema")
    if meta.get("status") not in STATUSES:
        raise ValueError("Status must be review, approved, skip or published")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", str(meta.get("id", ""))):
        raise ValueError("Invalid draft id")
    if rest.count(START) != 1 or rest.count(END) != 1:
        raise ValueError("Expected exactly one POST block")
    before, remainder = rest.split(START)
    body, notes = remainder.split(END)
    return meta, body.strip(), notes.strip()


def read_draft(path: Path):
    if path.is_symlink():
        raise ValueError("Draft must be a regular file")
    meta, body, notes = parse_draft(path.read_text("utf-8"))
    if path.stem != meta["id"]:
        raise ValueError("Draft id must match filename")
    return meta, body, notes


def write_draft(path: Path, meta: dict, body: str, notes: str) -> None:
    header = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=1000)
    atomic_text(path, f"---\n{header}---\n\n{START}\n{body.strip()}\n{END}\n\n{notes.strip()}\n")


def digest(meta: dict, text: str) -> str:
    fields = {key: meta.get(key) for key in ("id", "status", "source", "url", "news_published_at")}
    return hashlib.sha256((json.dumps(fields, sort_keys=True, ensure_ascii=False) + "\n" + text).encode()).hexdigest()


class GitStore:
    """Persist intent before send. A failed push stops publication; never force push."""
    def __init__(self, root: Path):
        self.root = root

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def sync(self) -> None:
        if self.git("status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError("Tracked local edits exist; publication stopped")
        self.git("fetch", "origin", "main")
        self.git("merge", "--ff-only", "origin/main")

    def persist(self) -> None:
        self.git("add", "data/drafts", "data/editorial-history.json", "data/editorial-state.json")
        if not self.git("diff", "--cached", "--name-only"):
            return
        self.git("commit", "-m", "Update editorial review queue and delivery receipts")
        # A simultaneous human edit may merge cleanly, but conflicts must stop the send.
        self.git("fetch", "origin", "main")
        self.git("merge", "--no-edit", "origin/main")
        self.git("push", "origin", "HEAD:main")

    def latest(self, path: Path) -> tuple[dict, str, str]:
        self.git("fetch", "origin", "main")
        return parse_draft(self.git("show", "origin/main:" + path.relative_to(self.root).as_posix()))
