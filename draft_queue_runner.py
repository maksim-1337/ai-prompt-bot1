from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import autopost_news as app
import main as news
import text_autopost_runner as editor
from news_sources import fetch_stories_resilient

CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@newsLightGG")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
QUEUE_PATH = Path("data/drafts/pending.json")
STATE_PATH = Path("data/autopost-state.json")
DRAFT_DELAY_MINUTES = int(os.getenv("DRAFT_DELAY_MINUTES", "60"))

news.fetch_stories = fetch_stories_resilient


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_queue() -> dict:
    if not QUEUE_PATH.exists():
        return {"schema_version": 1, "drafts": []}
    try:
        data = json.loads(QUEUE_PATH.read_text("utf-8"))
    except Exception:
        return {"schema_version": 1, "drafts": []}
    if data.get("schema_version") != 1 or not isinstance(data.get("drafts"), list):
        return {"schema_version": 1, "drafts": []}
    return data


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"schema_version": 1, "published": {}}
    try:
        data = json.loads(STATE_PATH.read_text("utf-8"))
    except Exception:
        return {"schema_version": 1, "published": {}}
    if data.get("schema_version") != 1 or not isinstance(data.get("published"), dict):
        return {"schema_version": 1, "published": {}}
    return data


def send_plain_text(text: str) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    text = text.strip()
    if not text:
        raise RuntimeError("Draft text is empty")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHANNEL,
            "text": text,
            "disable_web_page_preview": "true",
        },
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"Telegram HTTP {response.status_code}")
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram rejected request"))
    return payload["result"]


def commit_changes() -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "add", str(QUEUE_PATH), str(STATE_PATH)], check=True)
    status = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if status.returncode == 0:
        print("No queue/state changes to commit")
        return
    subprocess.run(["git", "commit", "-m", "Update NewsLightGG draft queue"], check=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)


def publish_one_due(queue: dict, state: dict) -> bool:
    now = utc_now()
    due = []
    for draft in queue["drafts"]:
        if draft.get("status") != "pending":
            continue
        try:
            publish_after = parse_iso(str(draft.get("publish_after", "")))
        except Exception:
            continue
        if publish_after <= now:
            due.append((publish_after, draft))

    if not due:
        return False

    _, draft = sorted(due, key=lambda item: item[0])[0]
    message = send_plain_text(str(draft.get("text", "")))
    draft["status"] = "published"
    draft["published_at"] = iso(now)
    draft["message_id"] = message.get("message_id")

    story_key = str(draft.get("story_id", ""))
    if story_key:
        state["published"][story_key] = {
            "title": draft.get("title", ""),
            "published_at": iso(now),
            "message_id": message.get("message_id"),
            "source": draft.get("source", ""),
            "source_url": draft.get("source_url", ""),
            "mode": "draft-delay-text",
        }
        state["published"] = dict(list(state["published"].items())[-500:])

    print(f"PUBLISHED DRAFT message_id={message.get('message_id')}")
    return True


def queued_ids(queue: dict) -> set[str]:
    return {
        str(item.get("story_id"))
        for item in queue["drafts"]
        if item.get("story_id") and item.get("status") in {"pending", "published", "skip"}
    }


def create_one_draft(queue: dict, state: dict) -> bool:
    stories = news.fetch_stories()
    news.add_confirmations(stories)
    for story in stories:
        story.score = news.score_story(story)

    blocked = set(state["published"]) | queued_ids(queue)
    candidates = [
        story for story in stories
        if story.score >= news.MIN_SCORE and app.story_id(story) not in blocked
    ]
    candidates.sort(key=lambda s: (editor.editorial_score(s), s.published), reverse=True)

    for story in candidates[:20]:
        news.enrich_story(story)
        if not editor.conversational_facts(story):
            continue

        now = utc_now()
        story_key = app.story_id(story)
        queue["drafts"].append({
            "story_id": story_key,
            "status": "pending",
            "created_at": iso(now),
            "publish_after": iso(now + timedelta(minutes=DRAFT_DELAY_MINUTES)),
            "title": story.title,
            "text": editor.format_text_post(story),
            "source": story.source,
            "source_url": story.link,
            "editorial_score": round(editor.editorial_score(story), 2),
            "edit_hint": "Можно изменить поле text. Чтобы отменить публикацию, поменяй status на skip.",
        })
        print(f"CREATED DRAFT: {story.title}")
        return True

    print("No suitable fresh story for a new draft")
    return False


def prune(queue: dict) -> None:
    # Keep all pending drafts plus only the latest completed history.
    pending = [d for d in queue["drafts"] if d.get("status") == "pending"]
    completed = [d for d in queue["drafts"] if d.get("status") != "pending"][-30:]
    queue["drafts"] = completed + pending


def main() -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    queue = load_queue()
    state = load_state()

    # Publish at most one overdue draft per hourly cycle, then prepare the next one.
    publish_one_due(queue, state)
    create_one_draft(queue, state)
    prune(queue)

    QUEUE_PATH.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", "utf-8")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")
    commit_changes()


if __name__ == "__main__":
    main()
