from __future__ import annotations

import json
from pathlib import Path

from . import http as requests


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, data=None, files=None):
        try:
            r = requests.post(self.base + method, data=data or {}, files=files, timeout=120)
            obj = r.json()
        except (requests.RequestException, ValueError):
            # Never echo a request URL containing the bot token.
            raise RuntimeError(f"Telegram {method}: network failure") from None
        if not r.ok or not obj.get("ok"):
            raise RuntimeError(f"Telegram {method}: HTTP {r.status_code}, code {obj.get('error_code')}")
        return obj["result"]

    def message(self, chat, text, keyboard=None):
        data = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}
        if keyboard:
            data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        return self.call("sendMessage", data)

    def preview(self, owner, path: Path, caption: str, keyboard):
        # sendDocument preserves exact bytes; Telegram video transcoding would break hashes.
        with path.open("rb") as f:
            return self.call("sendDocument", {
                "chat_id": owner, "caption": caption[:1000],
                "reply_markup": json.dumps({"inline_keyboard": keyboard}),
            }, {"document": ("preview.mp4", f, "video/mp4")})

    def download(self, file_id, target: Path):
        info = self.call("getFile", {"file_id": file_id})
        try:
            with requests.get(f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}",
                              stream=True, timeout=90) as r:
                if not r.ok:
                    raise RuntimeError("Telegram media download failed")
                size = 0
                with target.open("wb") as f:
                    for chunk in r.iter_content(65536):
                        size += len(chunk)
                        if size > 20_000_000:
                            raise RuntimeError("Telegram media exceeds download limit")
                        f.write(chunk)
        except requests.RequestException:
            raise RuntimeError("Telegram media download failed") from None
        return target

    def answer(self, callback_id, text):
        # Old callbacks may expire between scheduled runs. That is not an approval failure.
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
        except RuntimeError:
            pass
