import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import photo_publish as p


class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        self.post = {
            "schema_version": 1, "channel": "@newsLightGG", "status": "ready",
            "event_key": "example_space_announcement_2026_09_12", "event_date": "2026-09-12",
            "news_published_at": "2026-09-12T10:00:00Z", "verified_at": "2026-09-12T11:00:00Z",
            "title": "🚀 Заголовок тестовой новости", "virality": 7,
            "paragraphs": ["Это только тест проверки формата, он никогда не отправляется в канал. " * 4,
                           "Второй абзац нужен для проверки длины подписи и сохранения фактов. " * 4],
            "sources": [{"url": "https://example.com/news", "full_text_read": True, "independence_group": "primary"}],
            "verification": {"level": "official_announcement", "note": "Test fixture only"},
            "photo": {"url": "https://example.com/space.jpg", "source_page": "https://example.com/news",
                      "rights_url": "https://example.com/license", "rights": "test only", "subject": "Space",
                      "kind": "documentary", "visually_checked": True, "credit": ""}}
        self.state = {"schema_version": 1, "events": {}}
        self.message = {"message_id": 22, "date": int(self.now.timestamp()), "chat": {"id": p.CHANNEL_ID}, "photo": [{"file_id": "test"}]}

    def test_photo_only_and_receipt_order(self):
        snapshots = []
        def send(method, payload):
            self.assertEqual(snapshots[-1]["events"][self.post["event_key"]]["status"], "sending")
            self.assertEqual(method, "sendPhoto")
            self.assertEqual(payload["chat_id"], p.CHANNEL_ID)
            self.assertNotIn("https://", payload["caption"])
            return self.message
        self.assertTrue(p.publish(self.post, self.state, self.now, save=lambda s: snapshots.append(copy.deepcopy(s)), call=send))
        self.assertEqual(snapshots[-1]["events"][self.post["event_key"]]["status"], "published")

    def test_remote_intent_failure_never_sends(self):
        send = Mock()
        with self.assertRaises(RuntimeError):
            p.publish(self.post, self.state, self.now, save=Mock(side_effect=RuntimeError("push failed")), call=send)
        send.assert_not_called()

    def test_timeout_prevents_duplicate_and_future_sends(self):
        with self.assertRaises(TimeoutError):
            p.publish(self.post, self.state, self.now, save=Mock(), call=Mock(side_effect=TimeoutError()))
        self.assertEqual(self.state["events"][self.post["event_key"]]["status"], "uncertain")
        self.assertFalse(p.due(self.state, self.now + timedelta(hours=4)))
        self.assertFalse(p.publish(self.post, self.state, self.now, save=Mock(), call=Mock()))

    def test_three_hour_spacing_and_restart(self):
        p.publish(self.post, self.state, self.now, save=Mock(), call=Mock(return_value=self.message))
        restored = json.loads(json.dumps(self.state))
        self.assertFalse(p.due(restored, self.now + timedelta(hours=2, minutes=59)))
        self.assertTrue(p.due(restored, self.now + timedelta(hours=3)))
        send = Mock()
        self.assertFalse(p.publish(self.post, restored, self.now, save=Mock(), call=send))
        send.assert_not_called()

    def test_eight_posts_per_rolling_day(self):
        self.state["events"] = {
            str(i): {"status": "published", "published_at": (self.now - timedelta(hours=3 + i * 2)).isoformat()}
            for i in range(8)
        }
        self.assertFalse(p.due(self.state, self.now))

    def test_stale_unread_or_wrong_photo_is_rejected(self):
        mutations = [lambda x: x.update(news_published_at="2026-09-10T10:00:00Z"),
                     lambda x: x.update(channel="@someoneelse"),
                     lambda x: x["photo"].update(visually_checked=False),
                     lambda x: x["photo"].update(kind="video"),
                     lambda x: x["sources"][0].update(full_text_read=False),
                     lambda x: x["verification"].update(level="independent"),
                     lambda x: x["paragraphs"].append("https://example.com/"),
                     lambda x: x["photo"].update(kind="illustrative")]
        for mutation in mutations:
            post = copy.deepcopy(self.post)
            mutation(post)
            with self.assertRaises(ValueError):
                p.validate(post, self.now)

    def test_wrong_channel_blocks_connection(self):
        call = Mock(side_effect=[{"id": 123, "username": "PurpleHelperBot"}, {"id": 1, "username": "newsLightGG", "type": "channel"}])
        with self.assertRaises(RuntimeError):
            p.verify_connection(call)
        self.assertEqual(call.call_count, 2)

    def test_missing_or_corrupt_history_is_not_reset(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            with self.assertRaises(FileNotFoundError):
                p.read_state(path)
            path.write_text("oops")
            with self.assertRaises(ValueError):
                p.read_state(path)


if __name__ == "__main__":
    unittest.main()
