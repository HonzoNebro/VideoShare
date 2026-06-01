from bot.downloader import (
    DownloadVariant,
    TrimRange,
    VideoDownloader,
    VideoMetadata,
    _audio_quality_kbps,
    _classify_yt_dlp_error,
    _format_selector,
    _parse_js_runtimes,
    _parse_remote_components,
    calculate_video_bitrate_kbps,
    parse_trim_range,
    variant_cache_key,
)


def test_calculate_video_bitrate_kbps_leaves_room_for_audio() -> None:
    bitrate = calculate_video_bitrate_kbps(
        duration_seconds=60,
        target_bytes=50 * 1024 * 1024,
        audio_kbps=96,
    )

    assert bitrate is not None
    assert bitrate > 180


def test_calculate_video_bitrate_kbps_rejects_too_long_video() -> None:
    bitrate = calculate_video_bitrate_kbps(
        duration_seconds=60 * 60,
        target_bytes=5 * 1024 * 1024,
        audio_kbps=96,
    )

    assert bitrate is None


def test_metadata_cache_key_uses_extractor_and_video_id() -> None:
    first = VideoMetadata(
        video_id="abc123",
        title="Title",
        description=None,
        source_url="https://youtu.be/abc123?t=10",
        webpage_url="https://www.youtube.com/watch?v=abc123",
        uploader=None,
        extractor="Youtube",
        duration=30,
        estimated_size_bytes=None,
    )
    second = VideoMetadata(
        video_id="abc123",
        title="Title",
        description=None,
        source_url="https://youtube.com/shorts/abc123",
        webpage_url="https://www.youtube.com/watch?v=abc123",
        uploader=None,
        extractor="Youtube",
        duration=30,
        estimated_size_bytes=None,
    )

    assert first.cache_key == "youtube:abc123"
    assert first.cache_key == second.cache_key


def test_metadata_cache_key_falls_back_to_url_hash() -> None:
    metadata = VideoMetadata(
        video_id=None,
        title=None,
        description=None,
        source_url="https://example.com/video",
        webpage_url="https://example.com/video",
        uploader=None,
        extractor=None,
        duration=None,
        estimated_size_bytes=None,
    )

    assert metadata.cache_key.startswith("url:")


def test_variant_cache_key_includes_kind_and_quality() -> None:
    metadata = VideoMetadata(
        video_id="abc123",
        title=None,
        description=None,
        source_url="https://youtu.be/abc123",
        webpage_url="https://www.youtube.com/watch?v=abc123",
        uploader=None,
        extractor="Youtube",
        duration=30,
        estimated_size_bytes=None,
    )

    assert variant_cache_key(metadata, DownloadVariant("video", "high")) == "youtube:abc123:video-high"
    assert variant_cache_key(metadata, DownloadVariant("audio", "low")) == "youtube:abc123:audio-low"
    assert (
        variant_cache_key(
            metadata,
            DownloadVariant("video", "high"),
            TrimRange(start_seconds=83.16, end_seconds=130),
        )
        == "youtube:abc123:video-high:clip-8316-13000"
    )


def test_parse_trim_range_accepts_centiseconds() -> None:
    trim_range = parse_trim_range("01:23.16-02:10.00")

    assert trim_range.start_seconds == 83.16
    assert trim_range.end_seconds == 130
    assert trim_range.normalized_text == "01:23.16-02:10.00"


def test_parse_trim_range_accepts_single_decimal_centiseconds() -> None:
    trim_range = parse_trim_range("0:22.5-0:24.5")

    assert trim_range.start_seconds == 22.5
    assert trim_range.end_seconds == 24.5
    assert trim_range.normalized_text == "00:22.50-00:24.50"


def test_parse_trim_range_accepts_telegram_mention_prefix() -> None:
    trim_range = parse_trim_range("@compartirVideosbot 0:22.50-0:24.50")

    assert trim_range.start_seconds == 22.5
    assert trim_range.end_seconds == 24.5


def test_parse_trim_range_accepts_long_minutes() -> None:
    trim_range = parse_trim_range("123:45.67-124:00.00")

    assert trim_range.start_seconds == 7425.67
    assert trim_range.end_seconds == 7440


def test_parse_trim_range_rejects_invalid_ranges() -> None:
    for value in [
        "01:23:164-02:10:000",
        "01:60.00-02:00.00",
        "02:00.00-01:00.00",
    ]:
        try:
            parse_trim_range(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{value!r} should be rejected")


def test_video_variant_format_selectors() -> None:
    high = _format_selector(DownloadVariant("video", "high"), fallback_max_video_height=480)
    medium = _format_selector(DownloadVariant("video", "medium"), fallback_max_video_height=480)
    low = _format_selector(DownloadVariant("video", "low"), fallback_max_video_height=480)

    assert "height<=" not in high
    assert "height<=720" in medium
    assert "height<=360" in low


def test_audio_variant_format_and_quality() -> None:
    assert _format_selector(DownloadVariant("audio", "high"), fallback_max_video_height=480) == "bestaudio/best"
    assert _audio_quality_kbps(DownloadVariant("audio", "high")) == "192"
    assert _audio_quality_kbps(DownloadVariant("audio", "medium")) == "128"
    assert _audio_quality_kbps(DownloadVariant("audio", "low")) == "64"


def test_fetch_with_metadata_sync_passes_trim_through_pipeline(tmp_path, monkeypatch) -> None:
    downloader = VideoDownloader(download_dir=tmp_path, max_upload_bytes=1024)
    metadata = VideoMetadata(
        video_id="abc123",
        title=None,
        description=None,
        source_url="https://youtu.be/abc123",
        webpage_url="https://www.youtube.com/watch?v=abc123",
        uploader=None,
        extractor="Youtube",
        duration=30,
        estimated_size_bytes=None,
    )
    variant = DownloadVariant("video", "low")
    trim_range = TrimRange(start_seconds=1, end_seconds=2)
    calls: list[object] = []

    def fake_download(url, task_dir, selected_variant, progress, selected_trim):
        calls.append(("download", url, selected_variant, selected_trim))
        path = task_dir / "video.mp4"
        path.write_bytes(b"video")
        return path

    def fake_trim(path, selected_trim, selected_variant, progress, *, section_downloaded=False):
        calls.append(("trim", selected_trim, selected_variant))
        return path

    def fake_ensure(path, selected_metadata, selected_variant, progress):
        calls.append(("ensure", selected_metadata, selected_variant))
        return path

    monkeypatch.setattr(downloader, "_download", fake_download)
    monkeypatch.setattr(downloader, "_trim_download", fake_trim)
    monkeypatch.setattr(downloader, "_ensure_sendable", fake_ensure)

    result = downloader._fetch_with_metadata_sync(metadata, variant, trim_range)

    assert result.trim_range == trim_range
    assert calls[0] == ("download", metadata.source_url, variant, trim_range)
    assert calls[1] == ("trim", trim_range, variant)


def test_classify_youtube_bot_verification_error() -> None:
    message = "Sign in to confirm you’re not a bot. Use --cookies"

    assert "YTDLP_COOKIES_FILE" in _classify_yt_dlp_error(message)


def test_classify_youtube_js_challenge_error() -> None:
    message = "n challenge solving failed. Only images are available for download"

    assert "YTDLP_JS_RUNTIMES=deno" in _classify_yt_dlp_error(message)


def test_parse_js_runtimes_for_yt_dlp_api() -> None:
    assert _parse_js_runtimes("node") == {"node": {}}
    assert _parse_js_runtimes("node:/usr/bin/node,deno") == {
        "node": {"path": "/usr/bin/node"},
        "deno": {},
    }


def test_parse_remote_components_for_yt_dlp_api() -> None:
    assert _parse_remote_components("ejs:github,ejs:npm") == ["ejs:github", "ejs:npm"]
