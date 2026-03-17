"""Bilingual story-level diff generator for translation review."""

import json
from pathlib import Path


def compute_translation_diff(stories_translated_path: str | Path) -> dict:
    """Generate a bilingual story-level diff for translation review.

    Unlike correction diffs (word-level) or adaptation diffs (character-level),
    translation diffs show parallel German/Turkish text per story.
    Each story is one reviewable item with id "trans-s01", "trans-s02", etc.

    Returns dict with structure:
    {
        "episode_id": ...,
        "diff_type": "translation",
        "source_language": "de",
        "target_language": "tr",
        "stories": [{
            "item_id": "trans-s01",
            "story_id": "s01",
            "headline_de": ...,
            "headline_tr": ...,
            "text_de": ...,
            "text_tr": ...,
            "word_count_de": N,
            "word_count_tr": N,
            "category": ...,
            "story_type": ...,
        }],
        "summary": {
            "total_stories": N,
            "total_words_de": N,
            "total_words_tr": N,
            "compression_ratio": 0.90,
        },
        "warnings": ["Story s01: word ratio 0.40 (DE:100 -> TR:40) — possible summarization"],
    }
    """
    data = json.loads(Path(stories_translated_path).read_text(encoding="utf-8"))

    stories_diff = []
    total_words_de = 0
    total_words_tr = 0
    warnings = []

    for story in data.get("stories", []):
        words_de = len(story.get("text_de", "").split())
        words_tr = len(story.get("text_tr", "").split())
        total_words_de += words_de
        total_words_tr += words_tr

        # Flag anomalous compression/expansion
        if words_de > 0:
            ratio = words_tr / words_de
            if ratio < 0.5 or ratio > 1.5:
                warnings.append(
                    f"Story {story['story_id']}: word ratio {ratio:.2f} "
                    f"(DE:{words_de} \u2192 TR:{words_tr}) \u2014 possible "
                    f"{'summarization' if ratio < 0.5 else 'hallucination'}"
                )

        stories_diff.append(
            {
                "item_id": f"trans-{story['story_id']}",
                "story_id": story["story_id"],
                "headline_de": story.get("headline_de", ""),
                "headline_tr": story.get("headline_tr", ""),
                "text_de": story.get("text_de", ""),
                "text_tr": story.get("text_tr", ""),
                "word_count_de": words_de,
                "word_count_tr": words_tr,
                "category": story.get("category", ""),
                "story_type": story.get("story_type", ""),
            }
        )

    compression = total_words_tr / total_words_de if total_words_de > 0 else 1.0

    return {
        "episode_id": data.get("episode_id", ""),
        "diff_type": "translation",
        "source_language": "de",
        "target_language": "tr",
        "stories": stories_diff,
        "summary": {
            "total_stories": len(stories_diff),
            "total_words_de": total_words_de,
            "total_words_tr": total_words_tr,
            "compression_ratio": round(compression, 3),
        },
        "warnings": warnings,
    }
