import argparse
import json
from pathlib import Path

from .core import now, plan_from_story


def main():
    parser = argparse.ArgumentParser(description="NewsLight Video Studio")
    parser.add_argument("command", choices=["run", "demo"])
    parser.add_argument("--output", default="output/demo")
    parser.add_argument("--voice", action="store_true", help="Use real neural TTS in the demo")
    args = parser.parse_args()
    if args.command == "run":
        from .worker import run
        run()
    else:
        from .render import render
        sample = {"title": "Как новость превращается в короткое видео",
            "summary": "Система выбирает факты из поста и готовит вертикальный ролик. "
            "Русский голос сопровождают крупные субтитры и смена сцен. "
            "Перед отправкой в соцсети готовое видео приходит владельцу на одобрение.",
            "category": "tech", "source": "Демонстрация проекта, не новость", "link": "", "published": now()}
        plan = plan_from_story(sample)
        plan["label"] = "ДЕМО / NEWSLIGHT STUDIO"
        folder = Path(args.output)
        video, manifest = render(plan, folder, demo=not args.voice)
        (folder / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
        print(f"Demo created: {video}; {manifest['duration']:.1f}s; audio={manifest['audio']}")


if __name__ == "__main__":
    main()
