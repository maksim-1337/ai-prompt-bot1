import os
import unittest
from unittest.mock import Mock, patch

from studio.core import now, plan_from_story
from studio.worker import Worker


def sample_job():
    plan = plan_from_story({
        "title": "Компания показала новый сервис для коротких видео",
        "summary": "Сервис автоматически собирает вертикальные ролики из проверенного текста. "
                   "Перед публикацией владелец должен подтвердить готовую версию.",
        "source": "Test", "link": "https://example.com/news", "category": "tech", "published": now(),
    })
    return {"id": "direct-test", "state": "review", "revision": 1, "media_sha256": "abc",
            "plan": plan, "manifest": {"duration": 22.0}, "platform_meta": {},
            "deliveries": {}, "created": now(), "file_id": "telegram-file"}


class DirectModeTests(unittest.TestCase):
    def worker(self, extra=None):
        env = {"STUDIO_PUBLISHER": "direct", "STUDIO_TARGETS": "youtube,instagram,tiktok,facebook",
               "STUDIO_ACCOUNT_LABEL": "my accounts", "STUDIO_VISIBILITY": "public",
               "YOUTUBE_TOKEN_JSON": "yt", "INSTAGRAM_ACCESS_TOKEN": "ig", "INSTAGRAM_USER_ID": "ig-id",
               "TIKTOK_ACCESS_TOKEN": "tt", "FACEBOOK_PAGE_ACCESS_TOKEN": "fb"}
        env.update(extra or {})
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        store = Mock()
        store.data = {"owner": 123, "jobs": {}, "offset": 0, "paused": False}
        tg = Mock()
        return Worker(store, tg), store, tg

    def test_direct_mode_supports_all_requested_short_video_targets(self):
        worker, _, _ = self.worker()
        self.assertEqual(worker.targets, ["youtube", "instagram", "tiktok", "facebook"])

    def test_token_change_invalidates_approval_snapshot(self):
        worker, _, _ = self.worker()
        job = sample_job()
        first = worker.snapshot(job)
        with patch.dict(os.environ, {"TIKTOK_ACCESS_TOKEN": "rotated"}, clear=False):
            second = worker.snapshot(job)
        self.assertNotEqual(first["account_connection"], second["account_connection"])

    def test_tiktok_creator_settings_are_frozen_for_review(self):
        worker, _, _ = self.worker({"STUDIO_TARGETS": "tiktok"})
        job = sample_job()
        provider = Mock()
        provider.review_info.return_value = {
            "creator_username": "news", "creator_nickname": "News",
            "privacy": "PUBLIC_TO_EVERYONE", "privacy_options": ["PUBLIC_TO_EVERYONE"],
            "max_video_post_duration_sec": 300, "disable_comment": False,
            "disable_duet": False, "disable_stitch": False,
        }
        with patch("studio.worker.direct_provider", return_value=provider):
            worker.preflight(job)
        self.assertEqual(job["platform_meta"]["tiktok"]["creator_username"], "news")
        provider.review_info.assert_called_once_with("public")

    def test_restart_does_not_repeat_instagram_finalize(self):
        worker, store, tg = self.worker({"STUDIO_TARGETS": "instagram"})
        job = sample_job()
        job["state"] = "processing"
        job["deliveries"] = {"instagram": {"state": "finalizing", "remote_id": "container-1"}}
        worker.reconcile(job)
        self.assertEqual(job["deliveries"]["instagram"]["state"], "unknown")
        self.assertEqual(job["state"], "complete")
        tg.message.assert_called_once()
        self.assertGreaterEqual(store.save.call_count, 2)


if __name__ == "__main__":
    unittest.main()
