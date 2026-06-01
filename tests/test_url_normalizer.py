from bot.url_normalizer import normalize_url


def test_normalize_url_removes_tracking_and_fragment() -> None:
    assert (
        normalize_url("https://example.com/video/?utm_source=x&b=2&a=1#section")
        == "https://example.com/video?a=1&b=2"
    )


def test_normalize_youtube_watch_url() -> None:
    assert (
        normalize_url("https://www.youtube.com/watch?v=abc123&utm_source=x&t=30")
        == "https://www.youtube.com/watch?v=abc123"
    )


def test_normalize_youtube_short_and_shortener() -> None:
    assert normalize_url("https://youtu.be/abc123?si=track") == "https://www.youtube.com/watch?v=abc123"
    assert normalize_url("https://youtube.com/shorts/abc123?feature=share") == "https://www.youtube.com/watch?v=abc123"
