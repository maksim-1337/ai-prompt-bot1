from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def age_hours(value: str) -> float:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Publication timestamp must include timezone")
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def clean(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]*>", " ", value))
    value = re.sub(r"https?://\S+|www\.\S+|t\.me/\S+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def fingerprint(value: str) -> str:
    value = re.sub(r"\W+", " ", value.lower().replace("ё", "е")).strip()
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def plan_from_story(story: dict) -> dict:
    """Extract facts; never invent detail to fill a desired runtime."""
    title = clean(story["title"])
    if not 15 <= len(title) <= 220:
        raise ValueError("Headline length outside supported range")
    sentences = re.split(r"(?<=[.!?])\s+", clean(story.get("summary", "")))
    facts = []
    for sentence in sentences:
        if 25 <= len(sentence) <= 200 and fingerprint(sentence) != fingerprint(title):
            if sentence not in facts and title.lower() not in sentence.lower():
                facts.append(sentence)
    facts = facts[:3]
    if not facts:
        raise ValueError("Not enough source detail: headline alone is not a news video")
    # Keep a concise, source-grounded 15–40 second script.
    while len((title + " " + " ".join(facts)).split()) > 85 and len(facts) > 1:
        facts.pop()
    if len((title + " " + " ".join(facts)).split()) > 90:
        raise ValueError("Source sentences are too long for a short")
    categories = {
        "ai": ("ИСКУССТВЕННЫЙ ИНТЕЛЛЕКТ", "artificial intelligence computer"),
        "tech": ("ТЕХНОЛОГИИ", "smartphone technology"),
        "games": ("ИГРЫ", "video game controller"),
        "internet": ("ИНТЕРНЕТ", "social media smartphone"),
        "science": ("НАУКА", "space telescope"),
        "culture": ("КУЛЬТУРА", "cinema film projector"),
        "world": ("НОВОСТИ", "city aerial"),
    }
    category = story.get("category", "world")
    label, query = categories.get(category, categories["world"])
    return {
        "title": title, "facts": facts, "category": category, "label": label,
        "scenes": [{"text": text, "query": query} for text in [title, *facts]],
        "caption": title + "\n\n" + " ".join(facts) + "\n\n#новости #технологии #shorts",
        "sources": [{"name": story.get("source", ""), "url": story.get("link", "")}],
        "published": story["published"],
        "verification": "Автоматическая проверка фактов не выполнена. Сверь утверждения с источником.",
        "synthetic_voice": True,
    }


def publication_snapshot(job: dict, targets: list[str], account_label: str, mode: str, visibility: str) -> dict:
    return {"revision": job["revision"], "media_sha256": job["media_sha256"],
            "caption": job["plan"]["caption"], "title": job["plan"]["title"],
            "targets": targets, "account_label": account_label, "mode": mode,
            "visibility": visibility}


def decide(job: dict, action: str, revision: int, actor: int, owner: int) -> bool:
    if not owner or actor != owner or job["state"] != "review" or revision != job["revision"]:
        return False
    if action == "approve":
        if age_hours(job["plan"]["published"]) > 24:
            job["state"] = "expired"
            return False
        job["approval"] = {"actor": actor, "at": now(), "digest": digest(job["snapshot"])}
        job["state"] = "approved"
    elif action == "reject":
        job["state"] = "rejected"
    elif action == "remake":
        job["revision"] += 1
        job["state"] = "queued"
        job.pop("approval", None)
    else:
        return False
    return True


def may_publish(job: dict, snapshot: dict) -> bool:
    return (job["state"] == "approved" and bool(snapshot["targets"])
            and job.get("approval", {}).get("digest") == digest(snapshot)
            and -0.1 <= age_hours(job["plan"]["published"]) <= 24)
