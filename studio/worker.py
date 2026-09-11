from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

from .core import age_hours, clean, decide, digest, fingerprint, may_publish, now, plan_from_story, publication_snapshot
from .publishers import Ayrshare, YouTube, platform_result
from .render import render
from .state import Store, media_hash
from .telegram import Telegram


class Worker:
    def __init__(self, store, telegram):
        self.store, self.tg = store, telegram
        self.state = store.data
        self.channel = os.getenv("TELEGRAM_CHANNEL", "@newsLightGG")
        self.mode = os.getenv("STUDIO_PUBLISHER", "review")
        if self.mode not in ("review", "ayrshare", "youtube"):
            raise ValueError("STUDIO_PUBLISHER: review, ayrshare or youtube")
        self.targets = [] if self.mode == "review" else (
            ["youtube"] if self.mode == "youtube" else
            [x.strip() for x in os.getenv("STUDIO_TARGETS", "youtube,instagram,tiktok").split(",") if x.strip()])
        if len(self.targets) != len(set(self.targets)) or set(self.targets) - {"youtube", "instagram", "tiktok"}:
            raise ValueError("Invalid publishing targets")
        self.label = os.getenv("STUDIO_ACCOUNT_LABEL", "Аккаунты ещё не подключены")
        self.visibility = os.getenv("STUDIO_VISIBILITY", "public")
        if self.visibility not in ("public", "private"):
            raise ValueError("STUDIO_VISIBILITY must be public or private")
        if "instagram" in self.targets and self.visibility != "public":
            raise ValueError("Instagram Reels do not support private publication via this workflow")

    def snapshot(self, job):
        snap = publication_snapshot(job, self.targets, self.label, self.mode, self.visibility)
        credential = os.getenv("AYRSHARE_API_KEY", "") if self.mode == "ayrshare" else os.getenv("YOUTUBE_TOKEN_JSON", "")
        snap["account_connection"] = digest(credential)
        return snap

    def keyboard(self, job):
        suffix = f"{job['id']}:{job['revision']}"
        buttons = []
        if self.targets:
            buttons.append([{"text": "✅ Одобрить публикацию", "callback_data": "approve:" + suffix}])
        buttons.append([{"text": "🔄 Переделать", "callback_data": "remake:" + suffix},
                        {"text": "❌ Отклонить", "callback_data": "reject:" + suffix}])
        return buttons

    def summary(self):
        counts = Counter(j["state"] for j in self.state["jobs"].values())
        readable = {"queued": "В очереди", "review": "Ждут одобрения", "approved": "Одобрены",
                    "complete": "Обработаны", "failed": "Ошибка подготовки", "unknown": "Нужна проверка отправки",
                    "rejected": "Отклонены", "expired": "Устарели", "processing": "Площадки обрабатывают"}
        lines = [f"{readable.get(k,k)}: {v}" for k, v in counts.items()]
        return ("NewsLight Studio\n" + ("Пауза\n" if self.state["paused"] else "Работает\n")
                + f"Площадки: {', '.join(self.targets) or 'пока только предпросмотр'}\n" + "\n".join(lines))

    def updates(self):
        if self.tg.call("getWebhookInfo").get("url"):
            raise RuntimeError("Bot already has a webhook. Do not run a second update consumer")
        updates = self.tg.call("getUpdates", {"offset": self.state["offset"], "timeout": 0,
                                             "allowed_updates": json.dumps(["message", "callback_query"])})
        for update in updates:
            self.handle(update)
            self.state["offset"] = update["update_id"] + 1
            self.store.save()

    def handle(self, update):
        callback = update.get("callback_query")
        if callback:
            parts = callback.get("data", "").split(":")
            message = callback.get("message", {})
            if (len(parts) != 3 or not parts[2].isdigit()
                    or message.get("chat", {}).get("type") != "private"):
                return
            action, job_id, revision = parts
            job = self.state["jobs"].get(job_id)
            if not job or callback["from"]["id"] != self.state["owner"]:
                self.tg.answer(callback["id"], "Одобрять может только владелец канала")
                return
            if action == "approve" and job.get("snapshot") != self.snapshot(job):
                self.tg.answer(callback["id"], "Настройки изменились. Нужен новый предпросмотр")
                return
            accepted = decide(job, action, int(revision), callback["from"]["id"], self.state["owner"])
            self.store.save()
            self.tg.answer(callback["id"], "Принято" if accepted else "Кнопка устарела или решение уже принято")
            if accepted:
                self.tg.message(self.state["owner"], {"approve": "Одобрение сохранено. Начинаю отправку.",
                    "reject": "Ролик отклонён.", "remake": "Подготовлю новую версию для одобрения."}[action])
            return
        message = update.get("message", {})
        if message.get("chat", {}).get("type") != "private" or not message.get("from"):
            return
        actor = message["from"]["id"]
        text = message.get("text", "").strip()
        if not self.state["owner"] and text.startswith("/start"):
            # No first-visitor account takeover: Telegram must verify the CHANNEL OWNER.
            member = self.tg.call("getChatMember", {"chat_id": self.channel, "user_id": actor})
            if member.get("status") != "creator":
                return
            self.state["owner"] = actor
            self.store.save()
        if actor != self.state["owner"]:
            return
        if text.startswith(("/start", "/help")):
            self.tg.message(actor, "Это NewsLight Studio. Я пришлю готовые ролики для проверки.\n"
                "После одобрения отправлю в подключённые аккаунты.\n\n"
                "/status — очередь\n/pause — остановить подготовку и публикации\n/resume — продолжить\n"
                "/caption ID новый текст — изменить подпись и заново проверить ролик\n"
                "/retry ID — повторить подготовку после ошибки\n\n"
                "В GitHub Actions ответы появляются при следующем запуске, обычно через 15–30 минут.\n"
                "Базовый режим: только предпросмотр; аккаунты подключаются отдельно.")
        elif text == "/pause":
            self.state["paused"] = True
            self.store.save()
            self.tg.message(actor, "Подготовка и публикации приостановлены.")
        elif text == "/resume":
            self.state["paused"] = False
            self.store.save()
            self.tg.message(actor, "Работа продолжена.")
        elif text == "/status":
            self.tg.message(actor, self.summary())
        elif text.startswith("/caption "):
            parts = text.split(maxsplit=2)
            job = self.state["jobs"].get(parts[1]) if len(parts) == 3 else None
            if job and job["state"] in ("review", "approved"):
                job["plan"]["caption"] = clean(parts[2])[:1800]
                job["revision"] += 1
                job["state"] = "queued"
                job.pop("approval", None)
                self.store.save()
                self.tg.message(actor, "Подпись изменена. Пришлю новую версию для одобрения.")
        elif text.startswith("/retry "):
            job = self.state["jobs"].get(text.split()[-1])
            if job and job["state"] == "failed":
                job["state"] = "queued"
                self.store.save()
                self.tg.message(actor, "Повторю подготовку. Публикация потребует одобрения.")

    def intake(self):
        path = Path("data/video-inbox.json")
        if not path.exists():
            return
        stories = json.loads(path.read_text())
        today = now()[:10]
        for key, job in list(self.state["jobs"].items()):
            if job["state"] in ("complete", "expired", "rejected", "failed") and age_hours(job["created"]) > 168:
                del self.state["jobs"][key]
        today_count = sum(j["created"][:10] == today for j in self.state["jobs"].values())
        slots = max(0, min(4, int(os.getenv("STUDIO_DAILY_LIMIT", "2"))) - today_count)
        for story in sorted(stories, key=lambda s: s.get("score", 0), reverse=True):
            if not slots:
                break
            key = fingerprint(story["title"])
            if key in self.state["jobs"] or not -0.1 <= age_hours(story["published"]) <= 12:
                continue
            try:
                plan = plan_from_story(story)
            except ValueError:
                continue
            self.state["jobs"][key] = {"id": key, "revision": 1, "state": "queued", "created": now(),
                                        "plan": plan, "deliveries": {}}
            slots -= 1
        self.store.save()

    def prepare(self, job):
        job["state"] = "rendering"
        self.store.save()
        try:
            video, manifest = render(job["plan"], Path("output/studio") / job["id"] / str(job["revision"]),
                                     variant=job["revision"])
            job["media_sha256"] = media_hash(video)
            job["manifest"] = manifest
            job["snapshot"] = self.snapshot(job)
        except Exception as exc:
            job["state"], job["error"] = "failed", type(exc).__name__
            self.store.save()
            self.tg.message(self.state["owner"], f"Не удалось подготовить {job['id']}. Причина: {type(exc).__name__}.\n"
                            "Ролик не отправлялся в соцсети. После исправления: /retry " + job["id"])
            return
        source = job["plan"]["sources"][0]
        review = (f"Ролик {job['id']} · версия {job['revision']}\n\n{job['plan']['caption']}\n\n"
                  f"Площадки: {', '.join(self.targets) or 'только предпросмотр'}\n"
                  f"Аккаунты: {self.label}\nВидимость: {self.visibility}\n"
                  "Озвучка: синтезированный голос. Видеоряд: иллюстративные футажи / графика.\n"
                  f"{job['plan']['verification']}\nИсточник для проверки: {source['name']} {source['url']}\n\n"
                  "Это служебное сообщение. Источник не добавляется в подпись соцсетей.")
        if "tiktok" in self.targets:
            review += ("\nОдобрение включает согласие с Music Usage Confirmation TikTok: "
                       "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en\n"
                       "Комментарии, дуэты и stitch выключены. Музыка не добавляется.")
        job["state"] = "preview_sending"
        self.store.save()
        try:
            self.tg.message(self.state["owner"], review)
            result = self.tg.preview(self.state["owner"], video,
                f"{job['plan']['title']}\n\nВерсия {job['revision']} · {manifest['duration']:.0f} сек · 1080×1920\n"
                + ("Проверь видео и подпись выше, затем одобри отправку." if self.targets else
                   "Готовый MP4. Автопубликация ещё не подключена."), self.keyboard(job))
            job["file_id"] = result["document"]["file_id"]
            job["preview_message_id"] = result["message_id"]
            job["state"] = "review"
            self.store.save()
        except Exception:
            # An unconfirmed send must not cause an automatic second send.
            job["state"] = "unknown"
            self.store.save()
            raise

    def publish(self, job):
        if not may_publish(job, self.snapshot(job)):
            job["state"] = "queued" if age_hours(job["plan"]["published"]) <= 24 else "expired"
            job.pop("approval", None)
            self.store.save()
            return
        # Fail before remote writes if account credentials are missing.
        if self.mode == "ayrshare":
            provider = Ayrshare()
        elif self.mode == "youtube":
            provider = YouTube()
        else:
            return
        folder = Path("output/studio") / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        path = self.tg.download(job["file_id"], folder / "approved.mp4")
        if media_hash(path) != job["media_sha256"]:
            raise RuntimeError("Approved file hash mismatch; refusing to publish")
        if self.mode == "ayrshare" and not job.get("media_url"):
            job["media_url"] = provider.upload(path)
            self.store.save()
        for platform in self.targets:
            if platform in job["deliveries"]:
                continue
            # Persist intent before API request. On timeout/crash do not blindly duplicate it.
            delivery = {"state": "sending", "at": now()}
            job["deliveries"][platform] = delivery
            self.store.save()
            try:
                if self.mode == "ayrshare":
                    result = provider.publish(job, platform, job["media_url"], self.visibility)
                    delivery.update(platform_result(result, platform), remote_id=result["id"])
                else:
                    delivery.update(provider.publish(job, path, self.visibility))
            except Exception as exc:
                delivery.update(state="unknown", error=type(exc).__name__)
            self.store.save()
        job["state"] = "processing"
        self.store.save()

    def reconcile(self, job):
        for platform, delivery in job["deliveries"].items():
            if delivery["state"] == "sending":
                delivery["state"] = "unknown"
            elif delivery["state"] == "processing" and delivery.get("remote_id"):
                try:
                    delivery.update(platform_result(Ayrshare().status(delivery["remote_id"]), platform))
                except Exception:
                    continue
        self.store.save()
        if any(d["state"] == "processing" for d in job["deliveries"].values()):
            return
        job["state"] = "complete"
        self.store.save()
        labels = {"published": "Опубликовано", "uploaded": "Загружено; видимость зависит от YouTube/API",
                  "failed": "Ошибка площадки", "unknown": "Результат неизвестен: проверь аккаунт вручную"}
        report = ["Результат: " + job["plan"]["title"]]
        for p, d in job["deliveries"].items():
            report.append(f"{p}: {labels.get(d['state'], d['state'])} {d.get('url', '')}")
        self.tg.message(self.state["owner"], "\n".join(report))

    def run(self):
        self.updates()
        if not self.state["owner"]:
            print("Waiting for channel owner to send /start to the bot.")
            return
        for job in self.state["jobs"].values():
            if job["state"] == "rendering":
                job["state"] = "queued"  # no external effects have started
            elif job["state"] == "preview_sending":
                job["state"] = "unknown"
            if job["state"] == "processing":
                self.reconcile(job)
        if self.state["paused"]:
            self.store.save()
            return
        self.intake()
        for job in self.state["jobs"].values():
            if job["state"] in ("queued", "review", "approved") and age_hours(job["plan"]["published"]) > 24:
                job["state"] = "expired"
                self.store.save()
                continue
            if job["state"] == "review" and job.get("snapshot") != self.snapshot(job):
                job["revision"] += 1
                job["state"] = "queued"
            if job["state"] == "queued":
                self.prepare(job)
            if job["state"] == "approved":
                self.publish(job)
            if job["state"] == "processing":
                self.reconcile(job)


def run():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    Worker(Store(token), Telegram(token)).run()
