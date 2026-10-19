from bot.url_normalizer import normalize_url, reddit_extraction_url


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


def test_old_reddit_extraction_preserves_path_query_and_fragment() -> None:
    original = "https://old.reddit.com/r/videos/comments/abc/title/?context=3#comment"
    assert reddit_extraction_url(original) == "https://www.reddit.com/r/videos/comments/abc/title/?context=3#comment"
    assert normalize_url(original) == normalize_url("https://www.reddit.com/r/videos/comments/abc/title?context=3")


def test_reddit_extraction_does_not_rewrite_other_hosts() -> None:
    for url in (
        "https://old.reddit.com.example.org/r/videos/comments/abc",
        "https://v.redd.it/abc",
        "https://www.reddit.com/r/videos/comments/abc",
        "https://old.reddit.com@evil.example/r/videos/comments/abc",
    ):
        assert reddit_extraction_url(url) == url
