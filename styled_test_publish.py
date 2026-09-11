from datetime import datetime, timezone

import main as bot
import styled_publish  # applies styled post + HTML quote formatting

story = bot.Story(
    title="Apple выпустила первый складной iPhone за $1 999",
    link="https://www.reuters.com/business/retail-consumer/apples-foldable-iphone-poses-1999-question-who-is-it-2026-09-10/",
    source="Reuters",
    summary=(
        "Новый iPhone Duo раскрывается в большой экран и рассчитан на работу, видео и приложения с ИИ. "
        "Стартовая цена в США составляет $1 999, а версия с максимальной памятью стоит до $3 199. "
        "Аналитики ожидают высокий интерес на старте, но считают, что цена может оставить модель нишевой покупкой."
    ),
    published=datetime.now(timezone.utc),
    confirmations=2,
    category="tech",
)

video = bot.make_video(story)
bot.telegram_send_video(video, bot.build_post(story))
print("Styled test post published successfully.")
