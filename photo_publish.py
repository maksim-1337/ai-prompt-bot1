"""Publish the editor's verified photo queue. No RSS rewriting or video fallback."""
from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHANNEL = "@newsLightGG"
CHANNEL_ID = -1004433817207
STATE = Path("data/photo-publications.json")
QUEUE = Path("data/editorial-queue")
INTERVAL = timedelta(hours=3)


def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp needs timezone")
    return dt.astimezone(timezone.utc)


def public_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid URL")
    u = urllib.parse.urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.port not in (None, 443):
        raise ValueError("Public HTTPS URL required")
    if "." not in u.hostname or u.hostname.endswith((".local", ".internal", ".localhost")):
        raise ValueError("Public host required")
    try:
        address = ipaddress.ip_address(u.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Public host required")
    return value


def normalize(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-zа-я0-9 ]", " ", text.lower().replace("ё", "е"))).strip()


def caption(post):
    title, paragraphs = post["title"], post["paragraphs"]
    if not isinstance(title, str) or not 10 <= len(title) <= 130:
        raise ValueError("Invalid headline")
    if not isinstance(paragraphs, list) or not 2 <= len(paragraphs) <= 4:
        raise ValueError("Need 2-4 paragraphs")
    if any(not isinstance(p, str) or not p.strip() for p in paragraphs):
        raise ValueError("Empty paragraph")
    body = "\n\n".join(paragraphs)
    if not 400 <= len(body) <= 900:
        raise ValueError("Body must have 400-900 characters")
    credit = post["photo"].get("credit", "")
    plain = title + "\n\n" + body + ("\n\n" + credit if credit else "")
    if re.search(r"https?://|www\.|t\.me/", plain, re.I):
        raise ValueError("No external links in public caption")
    if len(plain.encode("utf-16-le")) // 2 > 1024:
        raise ValueError("Telegram photo caption too long")
    result = "<b>" + html.escape(title) + "</b>\n\n" + html.escape(body)
    if credit:
        result += "\n\n<i>" + html.escape(credit) + "</i>"
    return result


def validate(post, now):
    if post.get("channel") != CHANNEL or post.get("schema_version") != 1:
        raise ValueError("Wrong destination or schema")
    if not re.fullmatch(r"[a-z0-9_]{8,150}", post.get("event_key", "")):
        raise ValueError("Invalid event_key")
    if post.get("status") != "ready" or not 6 <= post.get("virality", 0) <= 10:
        raise ValueError("Not selected by editor")
    datetime.strptime(post["event_date"], "%Y-%m-%d")
    news_time = timestamp(post["news_published_at"])
    checked = timestamp(post["verified_at"])
    if not now - timedelta(hours=24) <= news_time <= now:
        raise ValueError("News outside 24-hour window")
    if not news_time <= checked <= now or checked < now - timedelta(hours=6):
        raise ValueError("Verification expired or inconsistent")
    sources = post.get("sources", [])
    if not sources:
        raise ValueError("No sources")
    for s in sources:
        public_url(s["url"])
        if s.get("full_text_read") is not True or not s.get("independence_group"):
            raise ValueError("Unread source")
    verification = post.get("verification", {})
    if not verification.get("note"):
        raise ValueError("Verification note required")
    if verification.get("level") == "independent":
        if len({s["independence_group"] for s in sources}) < 2:
            raise ValueError("Two independent groups required")
    elif verification.get("level") not in ("official_announcement", "single_authoritative"):
        raise ValueError("Unverified information")
    photo = post.get("photo", {})
    for field in ("url", "source_page", "rights_url"):
        public_url(photo[field])
    if photo.get("kind") not in ("documentary", "illustrative"):
        raise ValueError("Still photo required")
    if not photo.get("subject") or not photo.get("rights") or photo.get("visually_checked") is not True:
        raise ValueError("Photo subject and usage must be checked by editor")
    if photo.get("kind") == "illustrative" and "иллюстрац" not in (" ".join(post["paragraphs"]) + photo.get("credit", "")).lower():
        raise ValueError("Illustrative photo must be labelled")
    return caption(post)


def read_state(path=STATE):
    # Never recover a lost/corrupt history by silently starting empty.
    data = json.loads(path.read_text("utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("events"), dict):
        raise ValueError("Invalid publication history")
    for event in data["events"].values():
        if event.get("status") == "published":
            timestamp(event["published_at"])
    return data


def due(state, now):
    # Unknown outcomes reserve the slot until reconciled against Telegram.
    if any(e.get("status") in ("sending", "uncertain") for e in state["events"].values()):
        return False
    times = [timestamp(e["published_at"]) for e in state["events"].values() if e.get("status") == "published"]
    if sum(now - timedelta(hours=24) < t <= now for t in times) >= 8:
        return False
    return not times or now - max(times) >= INTERVAL


class TelegramRejected(RuntimeError):
    pass


def api(method, data):
    if method not in ("getMe", "getChat", "getChatMember", "sendPhoto"):
        raise ValueError("Unsupported method")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/" + method,
        data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        # Token-bearing URLs and external response bodies must not enter logs.
        if 400 <= exc.code < 500:
            raise TelegramRejected("Telegram rejected request (HTTP " + str(exc.code) + ")") from None
        raise RuntimeError("Telegram unavailable; outcome may be unknown") from None
    except Exception:
        raise RuntimeError("Telegram request incomplete; no automatic retry") from None
    if not payload.get("ok"):
        raise TelegramRejected("Telegram API rejected request")
    return payload["result"]


def verify_connection(call=api):
    me = call("getMe", {})
    if me.get("username", "").lower() != "purplehelperbot":
        raise RuntimeError("Unexpected bot")
    chat = call("getChat", {"chat_id": CHANNEL_ID})
    if chat.get("id") != CHANNEL_ID or chat.get("username", "").lower() != "newslightgg" or chat.get("type") != "channel":
        raise RuntimeError("Unexpected channel; publishing stopped")
    rights = call("getChatMember", {"chat_id": CHANNEL_ID, "user_id": me["id"]})
    if rights.get("status") != "administrator" or not rights.get("can_post_messages"):
        raise RuntimeError("Bot cannot publish")
    print("Connected: @PurpleHelperBot -> @newsLightGG; photo mode")


def save_state(data):
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temp, STATE)
    for command in (["git", "add", str(STATE)], ["git", "commit", "-m", "Record photo publication state"], ["git", "push", "origin", "HEAD:main"]):
        subprocess.run(command, check=True)


def publish(post, state, now, save=save_state, call=api):
    text = validate(post, now)
    key = post["event_key"]
    digest = hashlib.sha256(normalize(post["title"]).encode()).hexdigest()
    if key in state["events"] or any(e.get("headline_hash") == digest for e in state["events"].values()):
        return False
    if not due(state, now):
        return False
    record = {"status": "sending", "attempted_at": now.isoformat(), "headline_hash": digest,
              "title": post["title"], "photo_url": post["photo"]["url"], "channel": CHANNEL}
    state["events"][key] = record
    save(state)  # Persist remotely before the only send attempt.
    try:
        message = call("sendPhoto", {"chat_id": CHANNEL_ID, "photo": post["photo"]["url"],
                      "caption": text, "parse_mode": "HTML"})
        if not message.get("photo") or not message.get("message_id") or message.get("chat", {}).get("id") != CHANNEL_ID:
            raise RuntimeError("Incomplete photo receipt")
    except TelegramRejected as exc:
        record.update(status="rejected", error=str(exc))
        save(state)
        raise
    except Exception:
        record.update(status="uncertain", error="Check Telegram before any retry")
        save(state)
        raise
    record.update(status="published", published_at=datetime.fromtimestamp(message["date"], timezone.utc).isoformat(),
                  message_id=message["message_id"], url="https://t.me/newsLightGG/" + str(message["message_id"]))
    save(state)
    print("PUBLISHED " + record["url"])
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", metavar="POST_JSON", help="Validate locally without secrets, network or sending")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    if args.check:
        validate(json.loads(Path(args.check).read_text("utf-8")), now)
        print("Valid photo post")
        return
    state = read_state()
    verify_connection()
    if not due(state, now):
        print("Waiting for three-hour interval or unresolved receipt")
        return
    for path in sorted(QUEUE.glob("*.json")):
        if path.stem in state["events"]:
            continue
        try:
            post = json.loads(path.read_text("utf-8"))
            if path.stem != post["event_key"]:
                raise ValueError("Filename must match event_key")
            validate(post, now)
        except (ValueError, KeyError, TypeError):
            print("Skipped invalid or expired queue file: " + path.name)
            continue
        if publish(post, state, now):
            return
    print("No new verified photo post in queue; nothing sent")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
