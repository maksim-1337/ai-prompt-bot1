"""Bridge from a successful Telegram news post to the video production queue."""
import json
from pathlib import Path


def enqueue(story):
    path = Path("data/video-inbox.json")
    records = json.loads(path.read_text()) if path.exists() else []
    record = {"key": story.key, "title": story.title, "summary": story.summary,
              "source": story.source, "link": story.link, "category": story.category,
              "score": story.score, "published": story.published.isoformat()}
    if not any(item["key"] == record["key"] for item in records):
        records.append(record)
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(records[-80:], ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)
