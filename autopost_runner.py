from __future__ import annotations

import copy

import autopost_news as app
import main as news
from news_sources import fetch_stories_resilient

# Replace the Google-search-only source with a multi-source feed collector.
news.fetch_stories = fetch_stories_resilient

_original_commons_image = app.commons_image
GENERIC_IMAGE_QUERIES = {
    "ai": ["artificial intelligence computer", "computer server technology"],
    "tech": ["smartphone computer technology", "consumer electronics"],
    "games": ["video game controller gaming", "computer gaming"],
    "internet": ["internet social media smartphone", "computer network"],
    "science": ["space science laboratory", "astronomy telescope"],
    "culture": ["cinema movie camera", "film premiere cinema"],
    "world": ["technology city world", "digital news technology"],
}


def resilient_commons_image(story: news.Story):
    # First try the real headline/entities.
    result = _original_commons_image(story)
    if result:
        return result

    # If Commons has no safe image for a specific event, create an explicitly
    # illustrative branded cover from a generic category photo rather than skip.
    for query in GENERIC_IMAGE_QUERIES.get(story.category, GENERIC_IMAGE_QUERIES["world"]):
        proxy = copy.copy(story)
        proxy.title = query
        result = _original_commons_image(proxy)
        if result:
            print(f"Using generic illustrative Commons background: {query}")
            return result
    return None


app.commons_image = resilient_commons_image


if __name__ == "__main__":
    app.main()
