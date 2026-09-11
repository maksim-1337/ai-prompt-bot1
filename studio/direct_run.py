"""Production entrypoint: load encrypted state, renew OAuth, then run one studio cycle."""
from __future__ import annotations

import os

from .oauth import ensure_tiktok_access
from .state import Store
from .telegram import Telegram
from .worker import Worker


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    store = Store(token)
    ensure_tiktok_access(store)
    Worker(store, Telegram(token)).run()


if __name__ == "__main__":
    main()
