"""Launch the news bot with the current Telegram channel branding.

This small launcher lets us change the public channel username without
rewriting the whole news/video engine. The bot token still comes only from
GitHub Actions Secrets.
"""

import os
from pathlib import Path

CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@newsLightGG")
CHANNEL_SPOKEN_NAME = os.getenv("TELEGRAM_CHANNEL_SPOKEN_NAME", "Ньюс Лайт")

source = Path("main.py").read_text(encoding="utf-8")
source = source.replace("@historyXYT", CHANNEL)
source = source.replace("История икс вай ти", CHANNEL_SPOKEN_NAME)
source = source.replace("HistoryXYTNewsBot", "NewsLightGGBot")

namespace = {"__name__": "__main__", "__file__": "main.py"}
exec(compile(source, "main.py", "exec"), namespace)
