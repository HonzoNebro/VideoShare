from bot.formatting import (
    build_caption,
    extract_urls,
    text_mentions_bot,
    truncate_text,
)


def test_extract_urls_deduplicates_and_trims_punctuation() -> None:
    text = "Mira https://youtu.be/abc, y tambien https://example.com/video). https://youtu.be/abc"

    assert extract_urls(text) == ["https://youtu.be/abc", "https://example.com/video"]


def test_truncate_text_normalizes_whitespace() -> None:
    assert truncate_text("uno\n\n dos\t tres", 20) == "uno dos tres"
    assert truncate_text("abcdef", 5) == "ab..."


def test_build_caption_contains_escaped_metadata_and_link() -> None:
    caption = build_caption(
        title="Titulo <raro>",
        description="Descripcion con & simbolos",
        uploader="Canal",
        source_url="https://example.com/watch?v=1&x=2",
        max_description_chars=100,
    )

    assert "<b>Titulo &lt;raro&gt;</b>" in caption
    assert "Descripcion con &amp; simbolos" in caption
    assert 'href="https://example.com/watch?v=1&amp;x=2"' in caption


def test_build_caption_stays_within_telegram_limit() -> None:
    caption = build_caption(
        title="Titulo",
        description="x" * 5000,
        uploader=None,
        source_url="https://example.com/video",
        max_description_chars=5000,
    )

    assert len(caption) <= 1024


def test_text_mentions_bot_matches_username_case_insensitively() -> None:
    assert text_mentions_bot("@CompartirVideosBot https://youtu.be/abc", "compartirvideosbot")
    assert text_mentions_bot("mira @compartirvideosbot", "@CompartirVideosBot")
    assert not text_mentions_bot("mira compartirvideosbot", "compartirvideosbot")
