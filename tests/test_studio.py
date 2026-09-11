import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import InvalidToken

from studio.core import clean, decide, digest, may_publish, now, plan_from_story, publication_snapshot
from studio.publishers import Ayrshare, platform_result
from studio.state import Store, media_hash
from studio.worker import Worker


def sample():
    return {"title": "Компания представила новый игровой контроллер",
            "summary": "Устройство позволяет менять расположение кнопок. "
                       "В комплект входит кабель для подключения к компьютеру.",
            "source": "Пресс-релиз", "link": "https://example.com/news", "category": "games", "published": now()}


def job():
    j = {"id": "abc", "state": "review", "revision": 1, "media_sha256": "hash", "plan": plan_from_story(sample()),
         "deliveries": {}, "created": now(), "file_id": "telegram-file"}
    j["snapshot"] = publication_snapshot(j, ["youtube"], "Owner channel", "youtube", "public")
    return j


class SafetyTests(unittest.TestCase):
    def test_only_owner_can_approve(self):
        j = job()
        self.assertFalse(decide(j, "approve", 1, 900, 123))
        self.assertFalse(may_publish(j, j["snapshot"]))

    def test_unapproved_video_never_publishes(self):
        j = job()
        self.assertFalse(may_publish(j, j["snapshot"]))

    def test_duplicate_click_is_noop(self):
        j = job()
        self.assertTrue(decide(j, "approve", 1, 123, 123))
        self.assertFalse(decide(j, "approve", 1, 123, 123))
        self.assertTrue(may_publish(j, j["snapshot"]))

    def test_edited_caption_or_destination_requires_reapproval(self):
        j = job()
        decide(j, "approve", 1, 123, 123)
        for field, value in [("caption", "changed"), ("targets", ["tiktok"]), ("media_sha256", "different")]:
            changed = dict(j["snapshot"], **{field: value})
            self.assertFalse(may_publish(j, changed))

    def test_remake_invalidates_old_buttons(self):
        j = job()
        self.assertTrue(decide(j, "remake", 1, 123, 123))
        j["state"] = "review"
        self.assertFalse(decide(j, "approve", 1, 123, 123))

    def test_old_news_expires_even_if_approved(self):
        j = job()
        decide(j, "approve", 1, 123, 123)
        j["plan"]["published"] = (datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
        self.assertFalse(may_publish(j, j["snapshot"]))

    def test_missing_details_are_not_fabricated(self):
        s = sample()
        s["summary"] = ""
        with self.assertRaises(ValueError):
            plan_from_story(s)

    def test_no_links_or_html_in_public_caption(self):
        self.assertEqual(clean('<b>Факт</b> https://example.org?q=1'), "Факт")
        self.assertNotIn("http", plan_from_story(sample())["caption"])

    def test_pending_provider_id_is_not_published(self):
        result = {"status": "success", "postIds": [{"platform": "tiktok", "status": "success", "id": "pending"}]}
        self.assertEqual(platform_result(result, "tiktok")["state"], "processing")

    def test_partial_failure_has_per_platform_status(self):
        result = {"errors": [{"platform": "instagram", "code": 99}],
                  "postIds": [{"platform": "youtube", "status": "success", "id": "yt-1"}]}
        self.assertEqual(platform_result(result, "instagram")["state"], "failed")
        self.assertEqual(platform_result(result, "youtube")["state"], "published")

    def test_payload_is_repeatably_idempotent(self):
        j = job()
        a = Ayrshare.payload(j, "youtube", "https://example.org/short.mp4", "public")
        b = Ayrshare.payload(j, "youtube", "https://example.org/short.mp4", "public")
        self.assertEqual(a["idempotencyKey"], b["idempotencyKey"])
        self.assertTrue(a["youTubeOptions"]["shorts"])


class PersistenceTests(unittest.TestCase):
    def test_encrypted_restart_and_wrong_key_fail_closed(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"STUDIO_STATE_KEY": ""}):
            path = str(Path(root)/"queue.enc")
            store = Store("secret-token", path, remote=False)
            store.data["owner"] = 123
            store.data["jobs"]["abc"] = job()
            store.save()
            self.assertNotIn(b"telegram-file", Path(path).read_bytes())
            loaded = Store("secret-token", path, remote=False)
            self.assertEqual(loaded.data["owner"], 123)
            with self.assertRaises(InvalidToken):
                Store("other-token", path, remote=False)

    def test_corrupt_state_never_resets_to_empty(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"queue.enc"
            path.write_bytes(b"corrupted")
            with self.assertRaises(InvalidToken):
                Store("secret", str(path), remote=False)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"STUDIO_PUBLISHER": "ayrshare", "STUDIO_TARGETS": "youtube,instagram,tiktok",
                                          "STUDIO_ACCOUNT_LABEL": "owner", "STUDIO_VISIBILITY": "public"})
        self.env.start()
        self.store = Mock()
        self.store.data = {"owner": 123, "jobs": {}, "offset": 0, "paused": False}
        self.tg = Mock()
        self.worker = Worker(self.store, self.tg)

    def tearDown(self):
        self.env.stop()

    def test_wrong_person_cannot_claim_owner_role(self):
        self.store.data["owner"] = 0
        self.tg.call.return_value = {"status": "administrator"}
        self.worker.handle({"message": {"from": {"id": 900}, "chat": {"type": "private"}, "text": "/start"}})
        self.assertEqual(self.store.data["owner"], 0)
        self.tg.message.assert_not_called()

    def test_only_creator_can_initialize(self):
        self.store.data["owner"] = 0
        self.tg.call.return_value = {"status": "creator"}
        self.worker.handle({"message": {"from": {"id": 123}, "chat": {"type": "private"}, "text": "/start"}})
        self.assertEqual(self.store.data["owner"], 123)

    def test_network_timeout_does_not_repeat_post(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"short.mp4"
            path.write_bytes(b"approved video bytes")
            j = job()
            j["media_sha256"] = media_hash(path)
            j["snapshot"] = self.worker.snapshot(j)
            decide(j, "approve", 1, 123, 123)
            self.tg.download.return_value = path
            provider = Mock()
            provider.upload.return_value = "https://example.org/short.mp4"
            provider.publish.side_effect = TimeoutError()
            with patch("studio.worker.Ayrshare", return_value=provider):
                self.worker.publish(j)
                self.assertEqual(provider.publish.call_count, 3)
                j["state"] = "approved"  # Simulated restart from the last intent checkpoint.
                self.worker.publish(j)
                self.assertEqual(provider.publish.call_count, 3)
            self.assertTrue(all(d["state"] == "unknown" for d in j["deliveries"].values()))

    def test_checkpoint_failure_prevents_remote_post(self):
        j = job()
        j["snapshot"] = self.worker.snapshot(j)
        decide(j, "approve", 1, 123, 123)
        j["media_url"] = "https://example.org/short.mp4"
        self.store.save.side_effect = RuntimeError("checkpoint rejected")
        provider = Mock()
        with patch("studio.worker.Ayrshare", return_value=provider), patch("studio.worker.media_hash", return_value="hash"):
            with self.assertRaises(RuntimeError):
                self.worker.publish(j)
        provider.publish.assert_not_called()

    def test_regenerated_file_cannot_replace_approved_bytes(self):
        j = job()
        j["snapshot"] = self.worker.snapshot(j)
        decide(j, "approve", 1, 123, 123)
        with patch("studio.worker.Ayrshare") as factory, patch("studio.worker.media_hash", return_value="different"):
            with self.assertRaises(RuntimeError):
                self.worker.publish(j)
        factory.return_value.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
