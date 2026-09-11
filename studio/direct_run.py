"""Production entrypoint: load encrypted state, renew OAuth, then run one studio cycle.

Production video deliberately differs from the offline demo: it requires neural TTS
and applies a loudness stage suitable for phone speakers before a preview can be sent.
"""
from __future__ import annotations

import os

from .oauth import ensure_tiktok_access
from .state import Store
from .telegram import Telegram


def _install_production_audio_stage():
    """Add make-up gain and loudness normalization to rendered narration.

    The renderer already filters/compresses narration. This final stage raises perceived
    loudness to a consistent target without changing video generation or using paid APIs.
    """
    from . import render as render_module

    original_ffmpeg = render_module.ffmpeg

    def production_ffmpeg(args):
        args = list(args)
        if "-af" in args:
            index = args.index("-af") + 1
            base = str(args[index])
            # Voice remains dominant and clear on typical phone speakers.
            args[index] = base + ",volume=4dB,loudnorm=I=-14:TP=-1.2:LRA=6"
        return original_ffmpeg(args)

    render_module.ffmpeg = production_ffmpeg


def main():
    # Never fall back to the robotic offline demo voice in production.
    os.environ.setdefault("STUDIO_VOICE", "ru-RU-SvetlanaNeural")
    _install_production_audio_stage()

    # Import after the renderer is configured so Worker uses the production audio path.
    from .worker import Worker

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    store = Store(token)
    ensure_tiktok_access(store)
    Worker(store, Telegram(token)).run()


if __name__ == "__main__":
    main()
