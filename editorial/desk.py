from __future__ import annotations

import argparse
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .quality import MAX_AGE, assess, same_event, story_id, validate_post
from .rewrite import LocalEditor
from .sources import canonical_url, enrich, fetch_stories
from .store import GitStore, digest, load_json, read_draft, save_json, write_draft

CHANNEL = "@newsLightGG"


def now_utc():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if not result.tzinfo:
        raise ValueError("Timezone required")
    return result


def send_approved(meta: dict, text: str) -> dict:
    # Guard at the transport boundary as well as in the queue processor.
    if meta.get("status") != "approved":
        raise ValueError("Only approved drafts may reach Telegram")
    validate_post(text)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    try:
        # A timeout does not prove that Telegram did not deliver it. No retries.
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": CHANNEL, "text": text, "link_preview_options": {"is_disabled": True}},
            timeout=(8, 40),
        )
        response.raise_for_status()
        payload = response.json()
        message = payload.get("result", {})
        if payload.get("ok") is not True or type(message.get("message_id")) is not int or message["message_id"] <= 0:
            raise ValueError("No confirmed message_id")
        return message
    except (requests.RequestException, ValueError, KeyError):
        # Exception URLs can contain the bot token. Never expose them in logs.
        raise RuntimeError("Telegram delivery unconfirmed; inspect channel before any retry") from None


class Desk:
    def __init__(self, root: Path, persist=None, latest=None, sender=send_approved, clock=now_utc):
        self.root = root
        self.directory = root / "data/drafts/review"
        self.history_path = root / "data/editorial-history.json"
        self.state_path = root / "data/editorial-state.json"
        self.persist = persist or (lambda: None)
        self.latest = latest or read_draft
        self.sender = sender
        self.clock = clock
        if not self.history_path.exists() and any(self.directory.glob("*.md")):
            raise ValueError("Publication history missing; refusing to risk duplicate delivery")
        self.history = load_json(self.history_path, {"schema_version": 1, "events": {}})
        self.state = load_json(self.state_path, {"schema_version": 1, "collections": {}})
        for data, field in ((self.history, "events"), (self.state, "collections")):
            if data.get("schema_version") != 1 or not isinstance(data.get(field), dict):
                raise ValueError("Invalid history/state; refusing to reset it")

    def save(self):
        save_json(self.history_path, self.history)
        save_json(self.state_path, self.state)
        self.persist()

    def drafts(self):
        drafts, ids = [], set()
        for path in sorted(self.directory.glob("*.md")):
            meta, text, notes = read_draft(path)
            if meta["id"] in ids:
                raise ValueError("Duplicate draft id")
            ids.add(meta["id"])
            drafts.append((path, meta, text, notes))
        return drafts

    def migrate(self):
        legacy = self.root / "data/drafts/pending.json"
        if not legacy.exists():
            return
        queue = load_json(legacy, {})
        if queue.get("schema_version") != 1 or not isinstance(queue.get("drafts"), list):
            raise ValueError("Invalid legacy queue; migration stopped")
        old = load_json(self.root / "data/autopost-state.json", {"published": {}})
        for key, event in old.get("published", {}).items():
            self.history["events"].setdefault(key, dict(event, status="published", url=event.get("source_url", ""), legacy=True))
        for item in queue["drafts"]:
            key = str(item.get("story_id", ""))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", key):
                raise ValueError("Invalid legacy draft id")
            if item.get("status") == "published":
                self.history["events"].setdefault(key, {**item, "url": item.get("source_url", ""), "legacy": True})
                continue
            # Neither a timer nor a legacy ready/approved field is approval.
            meta = {
                "schema_version": 1, "id": key, "status": "skip" if item.get("status") == "skip" else "review",
                "title": item.get("title", ""), "source": item.get("source", ""), "url": item.get("source_url", ""),
                "created_at": item.get("created_at", ""), "news_published_at": "", "category": "technology",
                "editor_note": "Перенесено из pending. Проверь источник и заполни news_published_at: дата самой новости неизвестна.",
            }
            path = self.directory / f"{key}.md"
            if not path.exists():
                write_draft(path, meta, item.get("text", ""), "## Проверка редактором\n\nСтарый таймер отменён. Нужна проверка свежести и редакторская правка.")
        archive = self.root / "data/drafts/archive/legacy-pending.json"
        if archive.exists():
            raise ValueError("Legacy archive exists; refusing to overwrite")
        archive.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(archive)
        self.save()
        print("Legacy pending archived; all unpublished items require review")

    def publish(self) -> int:
        drafts = self.drafts()  # Validate the whole queue before any send.
        sent = 0
        for path, meta, text, notes in drafts:
            if meta["status"] != "approved":
                continue
            key = meta["id"]
            if key in self.history["events"]:
                event = self.history["events"][key]
                if event.get("status") == "published":
                    meta.update(status="published", message_id=event.get("message_id"), published_at=event.get("published_at"))
                    write_draft(path, meta, text, notes)
                    self.save()
                print(f"No resend for {key}: existing delivery receipt")
                continue
            validate_post(text)
            url = canonical_url(meta.get("url", ""))
            if not meta.get("source"):
                raise ValueError("Missing source")
            age = self.clock() - parse_time(meta.get("news_published_at"))
            if not -timedelta(minutes=5) <= age <= MAX_AGE:
                meta.update(status="review", editor_note="Материал устарел (старше 18 ч) или дата в будущем. Нужна повторная проверка.")
                write_draft(path, meta, text, notes)
                self.save()
                continue
            if any(event.get("url", event.get("source_url")) == url or same_event(meta.get("title", ""), event.get("title", "")) for event in self.history["events"].values()):
                meta.update(status="skip", editor_note="Дубль новости из истории отправок/попыток.")
                write_draft(path, meta, text, notes)
                self.save()
                continue
            approved_hash = digest(meta, text)
            event = {
                "status": "sending", "title": meta.get("title", ""), "draft": str(path.relative_to(self.root)),
                "channel": CHANNEL, "source": meta["source"], "url": url, "source_url": url,
                "attempted_at": iso(self.clock()), "approved_sha256": approved_hash, "text": text,
            }
            self.history["events"][key] = event
            self.save()  # Durably pushed BEFORE the network side effect.
            current_meta, current_text, _ = self.latest(path)
            if current_meta.get("status") != "approved" or digest(current_meta, current_text) != approved_hash:
                event["status"] = "cancelled_before_send"
                self.save()
                raise RuntimeError("Draft changed after approval; nothing was sent")
            try:
                message = self.sender(current_meta, current_text)
                if type(message.get("message_id")) is not int or message["message_id"] <= 0:
                    raise ValueError("Missing message_id")
            except Exception:
                event["status"] = "uncertain"
                self.save()
                raise RuntimeError("Delivery uncertain; automatic retry blocked by history") from None
            event.update(status="published", message_id=message["message_id"], published_at=iso(self.clock()),
                         telegram_url=f"https://t.me/newsLightGG/{message['message_id']}")
            meta.update(status="published", message_id=message["message_id"], published_at=event["published_at"])
            write_draft(path, meta, text, notes)
            self.save()  # After EACH message, before any RSS/model work.
            sent += 1
            print(f"Published approved draft {key}; message_id={message['message_id']}")
        print(f"Approved publications: {sent}")
        return sent

    def collect(self, fetch=fetch_stories, rewrite=None, enrich_story=enrich) -> int:
        now = self.clock()
        bucket = now.strftime("%Y-%m-%dT%H")
        if bucket in self.state["collections"]:
            print("This UTC hour has already been collected")
            return 0
        drafts = self.drafts()
        capacity = max(0, 3 - sum(meta["status"] in {"review", "approved"} for _, meta, _, _ in drafts))
        stories, feed_notes = fetch()
        for note in feed_notes:
            print(note)
        if not stories:
            raise RuntimeError("No dated stories from any feed; existing drafts untouched")
        blocked = [meta for _, meta, _, _ in drafts] + list(self.history["events"].values())
        rejected, candidates = Counter(), []
        for story in stories:
            score, topic, reason = assess(story, now)
            if not score:
                rejected[reason] += 1
                continue
            if any(item.get("url", item.get("source_url")) == story.url or same_event(story.title, item.get("title", "")) for item in blocked):
                rejected["уже в очереди или истории"] += 1
                continue
            candidates.append((score, story, topic, reason))
        candidates.sort(key=lambda item: (item[0], item[1].published), reverse=True)
        selected, sources = [], Counter()
        for item in candidates:
            if len(selected) >= capacity:
                break
            _, story, _, _ = item
            if sources[story.source] >= 2 or any(same_event(story.title, s[1].title) for s in selected):
                continue
            selected.append(item)
            sources[story.source] += 1
        editor = rewrite or LocalEditor()
        used = {text.split("\n\n")[-1] for _, _, text, _ in drafts}
        for score, story, topic, reason in selected:
            story = enrich_story(story)
            if not assess(story, now)[0]:
                rejected["полный текст не прошел фильтр"] += 1
                continue
            key = story_id(story)
            try:
                text = editor.compose(story, topic, used)
                validate_post(text)
                note = "Локальный пересказ. Сверь имена, смысл, оговорки и даты с источником; затем approved или skip."
                used.add(text.split("\n\n")[-1])
            except Exception as exc:
                text = "[ДОПИШИ главный факт своими словами]\n\n[ДОПИШИ 1–3 коротких абзаца по источнику ниже]\n\n[ДОПИШИ уместную короткую реплику]"
                note = f"Нужна ручная редактура: локальный пересказ не прошёл проверку ({type(exc).__name__})."
            meta = {
                "schema_version": 1, "id": key, "status": "review", "title": story.title,
                "category": topic, "created_at": iso(now), "news_published_at": iso(story.published),
                "source": story.source, "url": story.url, "editorial_score": score,
                "selection_reason": reason, "editor_note": note,
            }
            excerpt = story.summary.replace("<!--", "&lt;!--")
            notes = f"## Для редактора — не публикуется\n\n{note}\n\nИсточник: [{story.source}]({story.url})\n\nИсходный заголовок: {story.title}\n\n<details><summary>Факты источника для сверки (не готовый пост)</summary>\n\n> {excerpt}\n\n</details>"
            write_draft(self.directory / f"{key}.md", meta, text, notes)
        count = len(self.drafts()) - len(drafts)
        self.state["collections"][bucket] = {"created": count, "candidates": len(candidates), "rejected": dict(rejected), "feeds": feed_notes}
        self.state["collections"] = dict(list(self.state["collections"].items())[-168:])
        self.save()
        print(f"Collected {count} review drafts; active queue limit=3; candidates={len(candidates)}")
        return count


def main():
    parser = argparse.ArgumentParser(description="NewsLightGG editorial review")
    parser.add_argument("mode", choices=("check", "migrate", "collect", "publish", "cycle"), nargs="?", default="check")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--git", action="store_true")
    args = parser.parse_args()
    if args.mode in {"publish", "cycle"} and not args.git:
        parser.error("Live publishing requires --git (durable delivery receipts)")
    root = args.root.resolve()
    git = GitStore(root) if args.git else None
    if git:
        if os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("GITHUB_REF") != "refs/heads/main":
            parser.error("Live runs are only allowed by the main-branch GitHub workflow")
        git.sync()
    desk = Desk(root, persist=git.persist if git else None, latest=git.latest if git else None)
    if args.mode != "check":
        desk.migrate()
    if args.mode in {"publish", "cycle"}:
        desk.publish()
    if args.mode in {"collect", "cycle"}:
        desk.collect()
    if args.mode == "check":
        drafts = desk.drafts()
        for _, meta, text, _ in drafts:
            if meta["status"] == "approved":
                validate_post(text)
                canonical_url(meta.get("url", ""))
                parse_time(meta.get("news_published_at"))
        print("Draft statuses:", dict(Counter(meta["status"] for _, meta, _, _ in drafts)))


if __name__ == "__main__":
    main()
