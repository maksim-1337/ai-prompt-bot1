from datetime import datetime, timezone

from main import Story, build_post, make_video, telegram_send_video

story = Story(
    title="OpenAI допускает замедление развития ИИ из-за вопросов безопасности",
    link="https://www.reuters.com/business/altman-tells-staff-openai-is-open-slowing-ai-development-bloomberg-news-reports-2026-09-11/",
    source="Reuters / Bloomberg",
    summary=(
        "Сэм Альтман сообщил сотрудникам, что компания допускает замедление разработки своих ИИ-систем "
        "на фоне растущих опасений вокруг безопасности продвинутых моделей. По данным Bloomberg, это прозвучало "
        "на внутренней встрече OpenAI на этой неделе."
    ),
    published=datetime.now(timezone.utc),
    confirmations=2,
    category="ai",
)

video = make_video(story)
telegram_send_video(video, build_post(story))
print("Test post published successfully.")
