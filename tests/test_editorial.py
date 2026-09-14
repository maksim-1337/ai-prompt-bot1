import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from editorial.desk import Desk, iso, send_approved
from editorial.quality import assess, category, same_event, validate_post
from editorial.rewrite import check_rewrite, closer
from editorial.sources import Story, canonical_url, from_entry
from editorial.store import parse_draft, read_draft, save_json, write_draft

NOW = datetime(2026, 9, 13, 3, tzinfo=timezone.utc)
TEXT = 'Telegram запустил новую функцию для всех пользователей.\n\nОбновление позволяет управлять уведомлениями в общих чатах. Настройки доступны в приложении.\n\nУ групповых чатов появилась новая тема.'


class DeskTests(unittest.TestCase):
    def setUp(self):
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sender = Mock(return_value={"message_id": 42})
        self.desk = Desk(self.root, sender=self.sender, clock=lambda: NOW)

    def draft(self, status="review", key="draft0001", **fields):
        meta = {"schema_version": 1, "id": key, "status": status,
                "title": "Telegram запустил новую функцию", "source": "3DNews",
                "url": "https://3dnews.ru/1234", "news_published_at": iso(NOW - timedelta(hours=1))}
        meta.update(fields)
        path = self.desk.directory / (key + ".md")
        write_draft(path, meta, TEXT, "## Notes\n\nDo not send this section")
        return path

    def test_wait_skip_and_published_never_send(self):
        for i, status in enumerate(("review", "skip", "published")):
            self.draft(status, key=f"draft000{i}")
        self.assertEqual(self.desk.publish(), 0)
        self.sender.assert_not_called()

    def test_pending_unknown_missing_status_fail_closed(self):
        for status in ("pending", "APPROVED", "ready", "", None):
            with self.subTest(status=status):
                self.draft(status)
                with self.assertRaises(ValueError):
                    self.desk.publish()
        self.sender.assert_not_called()

    def test_approved_sends_exact_edited_body_and_records_receipt(self):
        path = self.draft("approved")
        meta, _, notes = read_draft(path)
        edited = TEXT.replace("в общих чатах", "в групповых чатах")
        write_draft(path, meta, edited, notes)
        snapshots = []
        self.desk.persist = lambda: snapshots.append(copy.deepcopy(self.desk.history))
        self.assertEqual(self.desk.publish(), 1)
        self.assertEqual(self.sender.call_args.args[1], edited)
        self.assertEqual(snapshots[0]["events"][meta["id"]]["status"], "sending")
        event = snapshots[-1]["events"][meta["id"]]
        self.assertEqual((event["message_id"], event["source"], event["url"]), (42, "3DNews", meta["url"]))
        self.assertEqual(read_draft(path)[0]["status"], "published")
        self.assertNotIn("Notes", event["text"])

    def test_restart_does_not_repeat_or_reapprove(self):
        path = self.draft("approved")
        self.desk.publish()
        meta, body, notes = read_draft(path)
        meta["status"] = "approved"
        write_draft(path, meta, body, notes)
        Desk(self.root, sender=self.sender, clock=lambda: NOW).publish()
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(read_draft(path)[0]["status"], "published")

    def test_failed_intent_persistence_means_no_network(self):
        self.draft("approved")
        self.desk.persist = Mock(side_effect=RuntimeError("push rejected"))
        with self.assertRaises(RuntimeError):
            self.desk.publish()
        self.sender.assert_not_called()

    def test_changed_approval_between_commit_and_send(self):
        path = self.draft("approved")
        meta, text, notes = read_draft(path)
        meta["status"] = "skip"
        self.desk.latest = lambda _: (meta, text, notes)
        with self.assertRaises(RuntimeError):
            self.desk.publish()
        self.sender.assert_not_called()

    def test_edited_body_after_approval_stops_send(self):
        path = self.draft("approved")
        meta, text, notes = read_draft(path)
        self.desk.latest = lambda _: (meta, text + " Новая версия.", notes)
        with self.assertRaises(RuntimeError):
            self.desk.publish()
        self.sender.assert_not_called()

    def test_cancelled_before_send_can_be_reapproved(self):
        path = self.draft("approved")
        meta, text, notes = read_draft(path)
        cancelled = dict(meta, status="skip")
        self.desk.latest = lambda _: (cancelled, text, notes)
        with self.assertRaises(RuntimeError):
            self.desk.publish()
        self.sender.assert_not_called()
        self.desk.latest = read_draft
        self.assertEqual(self.desk.publish(), 1)
        self.assertEqual(self.desk.history["cancelled_attempts"][0]["status"], "cancelled_before_send")
        self.assertEqual(self.sender.call_count, 1)

    def test_timeout_and_missing_id_never_retry(self):
        for mode in ("timeout", "missing"):
            with self.subTest(mode=mode):
                key = "timeout01" if mode == "timeout" else "missing01"
                self.draft("approved", key=key, title=key, url=f"https://3dnews.ru/{key}")
                self.sender.side_effect = TimeoutError() if mode == "timeout" else None
                self.sender.return_value = {}
                with self.assertRaises(RuntimeError):
                    self.desk.publish()
                self.assertEqual(self.desk.history["events"][key]["status"], "uncertain")
                count = self.sender.call_count
                Desk(self.root, sender=self.sender, clock=lambda: NOW).publish()
                self.assertEqual(self.sender.call_count, count)

    def test_receipt_write_failure_leaves_durable_intent(self):
        self.draft("approved")
        def save():
            if self.sender.called:
                raise RuntimeError("push rejected after send")
        self.desk.persist = save
        with self.assertRaises(RuntimeError):
            self.desk.publish()
        # Simulate recovering only the remote intent, losing local final receipt.
        record = self.desk.history["events"]["draft0001"]
        record["status"] = "sending"
        save_json(self.desk.history_path, self.desk.history)
        self.draft("approved")
        Desk(self.root, sender=self.sender, clock=lambda: NOW).publish()
        self.assertEqual(self.sender.call_count, 1)

    def test_stale_approved_returns_to_review(self):
        path = self.draft("approved", news_published_at=iso(NOW - timedelta(hours=24)))
        self.desk.publish()
        self.sender.assert_not_called()
        self.assertEqual(read_draft(path)[0]["status"], "review")

    def test_duplicate_url_with_changed_id_never_sends_twice(self):
        self.draft("approved")
        self.desk.publish()
        path = self.draft("approved", key="duplicate02", title="Другой заголовок")
        self.desk.publish()
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(read_draft(path)[0]["status"], "skip")

    def test_broken_history_is_not_reset(self):
        self.desk.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.desk.history_path.write_text("{broken")
        with self.assertRaises(ValueError):
            Desk(self.root)

    def test_missing_history_with_existing_queue_fails_closed(self):
        self.draft("approved")
        with self.assertRaises(ValueError):
            Desk(self.root, sender=self.sender)
        self.sender.assert_not_called()

    def test_migration_never_imports_approval_or_timer(self):
        legacy = self.root / "data/drafts/pending.json"
        old = {"schema_version": 1, "drafts": [{"story_id": "legacy001", "status": "pending", "publish_after": "2000-01-01T00:00:00Z", "text": TEXT}]}
        save_json(legacy, old)
        self.desk.migrate()
        self.desk.migrate()
        self.assertFalse(legacy.exists())
        self.assertEqual(read_draft(self.desk.directory / "legacy001.md")[0]["status"], "review")
        self.desk.publish()
        self.sender.assert_not_called()
        archive = self.root / "data/drafts/archive/legacy-pending.json"
        self.assertEqual(json.loads(archive.read_text()), old)

    def test_hourly_limit_quality_and_no_publication(self):
        titles = ["Telegram впервые запустил чаты для миллиона пользователей", "NASA впервые обнаружили следы воды на экзопланете", "Nintendo выпустила новую игру для миллионов игроков", "Фильм установил мировой рекорд в прокате"]
        stories = [Story(t, "Впервые появились новые возможности для миллионов людей. Подробности подтвердили участники события в опубликованном сообщении.", "3DNews" if i % 2 else "iXBT", f"https://3dnews.ru/{i}", NOW) for i, t in enumerate(titles)]
        fetch = lambda: (stories, ["online"])
        model = Mock()
        model.compose.return_value = TEXT
        self.assertEqual(self.desk.collect(fetch, model, lambda s: s), 3)
        self.assertEqual(self.desk.collect(fetch, model, lambda s: s), 0)
        self.assertTrue(all(m["status"] == "review" for _, m, _, _ in self.desk.drafts()))
        self.sender.assert_not_called()

    def test_review_capacity_preserves_existing_edits(self):
        path = self.draft()
        before = path.read_bytes()
        self.desk.collect(lambda: ([Story("NASA впервые открыли новый мир", "Подробности открытия подтвердили исследователи. Новая планета находится в далекой системе и ранее не была известна.", "3DNews", "https://3dnews.ru/other", NOW)], []), Mock(compose=lambda *_: TEXT), lambda s: s)
        self.assertEqual(path.read_bytes(), before)
        self.assertLessEqual(len(self.desk.drafts()), 3)

    def test_stale_reviews_free_capacity_without_losing_text(self):
        paths = [self.draft("review", key=f"oldrev00{i}", news_published_at=iso(NOW - timedelta(days=1))) for i in range(3)]
        story = Story("NASA впервые открыли новую планету", "Подробности открытия подтвердили исследователи. Результаты наблюдений впервые опубликованы в научном журнале.", "3DNews", "https://3dnews.ru/new", NOW)
        self.assertEqual(self.desk.collect(lambda: ([story], []), Mock(compose=lambda *_: TEXT), lambda s: s), 1)
        for path in paths:
            meta, text, _ = read_draft(path)
            self.assertEqual(meta["status"], "skip")
            self.assertEqual(text, TEXT)

    def test_model_failure_creates_review_not_copied_post(self):
        s = Story("NASA впервые обнаружили воду на экзопланете", "Подробности открытия подтвердили исследователи. Результаты наблюдений впервые опубликованы в научном журнале.", "3DNews", "https://3dnews.ru/planet", NOW)
        model = Mock()
        model.compose.side_effect = RuntimeError("unavailable")
        self.desk.collect(lambda: ([s], []), model, lambda s: s)
        _, meta, text, _ = self.desk.drafts()[0]
        self.assertEqual(meta["status"], "review")
        with self.assertRaises(ValueError):
            validate_post(text)


class QualityTests(unittest.TestCase):
    def story(self, title, **kwargs):
        data = dict(title=title, summary="Новое событие впервые затронет миллионы людей. Подробности подтверждены в опубликованном сообщении редакции.", source="3DNews", url="https://3dnews.ru/test", published=NOW)
        data.update(kwargs)
        return Story(**data)

    def test_reject_editorial_regressions(self):
        for title in ("Я написал честный сайт с кейсами CS2", "Норрис сенсационно выиграл квалификацию Формулы-1", "Министр провел заседание правительства", "Индекс Мосбиржи вырос впервые за месяц", "Biostar рекламирует новую видеокарту", "Вы не поверите, что сделала нейросеть", "Лучшие смартфоны купить со скидкой"):
            with self.subTest(title=title):
                self.assertEqual(assess(self.story(title), NOW)[0], 0)

    def test_sport_not_games_and_politics_allowed(self):
        s = self.story("Футболист установил мировой рекорд в финале чемпионата мира")
        self.assertEqual(category(s), "sports")
        self.assertGreater(assess(s, NOW)[0], 0)
        self.assertGreater(assess(self.story("Парламент принял закон о запрете массовой слежки"), NOW)[0], 0)

    def test_missing_old_future_dates_rejected(self):
        for date in (NOW - timedelta(days=1), NOW + timedelta(hours=1), NOW.replace(tzinfo=None)):
            self.assertEqual(assess(self.story("NASA впервые открыли новую планету", published=date), NOW)[0], 0)
        self.assertIsNone(from_entry("3DNews", {"title": "Новость без достоверной даты", "link": "https://3dnews.ru/123"}))

    def test_url_allowlist_and_tracking(self):
        self.assertEqual(canonical_url("https://www.3dnews.ru/123/?utm_source=a#x"), "https://3dnews.ru/123")
        for url in ("http://3dnews.ru/a", "https://3dnews.ru.evil.example/a", "https://habr.com/ru/articles/1", "https://127.0.0.1/a"):
            with self.assertRaises(ValueError):
                canonical_url(url)

    def test_cross_source_headlines_deduplicate(self):
        self.assertTrue(same_event("OpenAI отложила выход на IPO из-за вопросов безопасности", "OpenAI не выйдет на IPO в этом году, поскольку считает момент неподходящим"))
        self.assertTrue(same_event("Blizzard анонсировала Diablo V — релиз в 2029 году", "Blizzard неожиданно анонсировала Diablo V — игра выйдет весной 2029 года"))

    def test_no_broken_html_links_or_placeholders(self):
        validate_post(TEXT)
        for bad in (TEXT + " https://example.org", TEXT.replace("Telegram", "&quot;Telegram&quot;"), TEXT.replace("Настройки доступны в приложении.", "Настройки доступны…"), "[ДОПИШИ]\n\nФакты.\n\nКонец."):
            with self.assertRaises(ValueError):
                validate_post(bad)

    def test_duplicate_yaml_status_fails(self):
        raw = '---\nschema_version: 1\nid: draft0001\nstatus: review\nstatus: approved\n---\n<!-- POST -->\n' + TEXT + '\n<!-- /POST -->'
        with self.assertRaises(ValueError):
            parse_draft(raw)

    def test_rewrite_numbers_names_negation_are_preserved(self):
        for original, rewritten in (("Apple выпустила 3 модели телефона.", "Apple представила 4 новых телефона."), ("Telegram не будет закрывать канал.", "Telegram будет закрывать этот канал."), ("OpenAI представила новую версию модели.", "Google выпустила обновленную модель.")):
            with self.assertRaises(ValueError):
                check_rewrite(original, rewritten)

    def test_tragedy_has_no_ironic_closer(self):
        s = self.story("В результате атаки погибли люди")
        self.assertEqual(closer(s, "politics", set()), "Здесь важнее дождаться проверенных подробностей.")

    def test_transport_rejects_every_unapproved_status(self):
        with patch("editorial.desk.requests.post") as post:
            for status in ("pending", "review", "skip", "published", "APPROVED", None):
                with self.assertRaises(ValueError):
                    send_approved({"status": status}, TEXT)
            post.assert_not_called()

    def test_transport_plain_text_target_and_no_media(self):
        response = Mock()
        response.json.return_value = {"ok": True, "result": {"message_id": 42}}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), patch("editorial.desk.requests.post", return_value=response) as post:
            send_approved({"status": "approved"}, TEXT)
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["chat_id"], "@newsLightGG")
            self.assertEqual(payload["text"], TEXT)
            self.assertNotIn("parse_mode", payload)
            self.assertTrue(post.call_args.args[0].endswith("/sendMessage"))

    def test_transport_errors_do_not_leak_token(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret-test"}), patch("editorial.desk.requests.post", side_effect=requests.Timeout("secret-test")):
            with self.assertRaises(RuntimeError) as error:
                send_approved({"status": "approved"}, TEXT)
            self.assertNotIn("secret-test", str(error.exception))

    def test_workflow_only_one_live_publisher_no_paid_api(self):
        root = Path(__file__).resolve().parents[1]
        workflows = list((root / ".github/workflows").glob("*.yml"))
        live = [p for p in workflows if "secrets.TELEGRAM_BOT_TOKEN" in p.read_text()]
        self.assertEqual([p.name for p in live], ["news-autopilot.yml"])
        workflow = live[0].read_text()
        self.assertIn("data/editorial-queue/**", workflow)
        self.assertIn("run: python photo_publish.py", workflow)
        self.assertNotIn("editorial.desk publish", workflow)
        self.assertIn("47 */3 * * *", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("OPENAI_API_KEY", workflow)
        self.assertNotIn("DRAFT_DELAY", workflow)


if __name__ == "__main__":
    unittest.main()
