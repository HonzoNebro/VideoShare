import shutil
import subprocess

import pytest

from bot.downloader import (
    AudioUnavailableError,
    DownloadError,
    DownloadVariant,
    ImageVariant,
    MediaStreams,
    TrimRange,
    VideoDownloader,
    VideoMetadata,
    VideoResult,
    _apply_image_quality,
    _audio_quality_kbps,
    _audio_is_silent,
    _classify_yt_dlp_error,
    _detect_content_type,
    _format_selector,
    _download_images,
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

    assert variant_cache_key(metadata, DownloadVariant("video", "high")) == "youtube:abc123:video-high:audio-validated-v1"
    assert variant_cache_key(metadata, DownloadVariant("audio", "low")) == "youtube:abc123:audio-low:audio-validated-v1"
    assert (
        variant_cache_key(
            metadata,
            DownloadVariant("video", "high"),
            TrimRange(start_seconds=83.16, end_seconds=130),
        )
        == "youtube:abc123:video-high:clip-8316-13000:audio-validated-v1"
    )


def test_image_variant_allows_first_or_all_only() -> None:
    assert ImageVariant("high", 1).cache_suffix == "image-high-count1"
    assert ImageVariant("low", -1).cache_suffix == "image-low-countall"

    for count in [0, 5, 10]:
        try:
            ImageVariant("medium", count)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{count} should be rejected")


def test_detect_content_type_keeps_video_as_video() -> None:
    is_video, is_image, image_count, image_urls = _detect_content_type({
        "duration": 10,
        "formats": [{"vcodec": "h264", "height": 720, "ext": "mp4"}],
        "thumbnails": [{"url": "https://example.com/thumb.jpg", "width": 1200, "height": 800}],
    })

    assert is_video is True
    assert is_image is False
    assert image_count == 0
    assert image_urls == []


def test_detect_content_type_detects_single_image() -> None:
    is_video, is_image, image_count, image_urls = _detect_content_type({
        "url": "https://example.com/image.webp",
        "ext": "webp",
        "duration": None,
    })

    assert is_video is False
    assert is_image is True
    assert image_count == 1
    assert image_urls == ["https://example.com/image.webp"]


def test_detect_content_type_preserves_album_entry_order() -> None:
    is_video, is_image, image_count, image_urls = _detect_content_type({
        "entries": [
            {"url": "https://example.com/1.jpg", "ext": "jpg"},
            {"url": "https://example.com/2.jpg", "ext": "jpg"},
            {"url": "https://example.com/1.jpg", "ext": "jpg"},
        ]
    })

    assert is_video is False
    assert is_image is True
    assert image_count == 2
    assert image_urls == ["https://example.com/1.jpg", "https://example.com/2.jpg"]


def test_detect_content_type_detects_mixed_video_and_images() -> None:
    is_video, is_image, image_count, image_urls = _detect_content_type({
        "formats": [{"vcodec": "h264", "height": 720, "ext": "mp4"}],
        "entries": [{"url": "https://example.com/1.jpg", "ext": "jpg"}],
    })

    assert is_video is True
    assert is_image is True
    assert image_count == 1
    assert image_urls == ["https://example.com/1.jpg"]


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


def test_ensure_sendable_rejects_video_without_audio(tmp_path, monkeypatch) -> None:
    downloader = VideoDownloader(download_dir=tmp_path, max_upload_bytes=1024)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    metadata = VideoMetadata(
        video_id="id", title=None, description=None, source_url="https://example.com/video",
        webpage_url="https://example.com/video", uploader=None, extractor="Example",
        duration=10, estimated_size_bytes=None,
    )
    monkeypatch.setattr(
        "bot.downloader._probe_media_streams",
        lambda path: MediaStreams(video_codec="h264", audio_codec=None, duration_seconds=10),
    )

    try:
        downloader._ensure_sendable(source, metadata, DownloadVariant("video", "medium"), None)
    except DownloadError as exc:
        assert "no contiene una pista de audio" in str(exc)
    else:
        raise AssertionError("A video without audio must not be sent")


def test_ensure_sendable_keeps_compatible_mp4_without_transcoding(tmp_path, monkeypatch) -> None:
    downloader = VideoDownloader(download_dir=tmp_path, max_upload_bytes=1024)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    metadata = VideoMetadata(
        video_id="id", title=None, description=None, source_url="https://example.com/video",
        webpage_url="https://example.com/video", uploader=None, extractor="Example",
        duration=10, estimated_size_bytes=None,
    )
    monkeypatch.setattr(
        "bot.downloader._probe_media_streams",
        lambda path: MediaStreams(video_codec="h264", audio_codec="aac", duration_seconds=10),
    )
    monkeypatch.setattr("bot.downloader._audio_is_silent", lambda path: False)
    monkeypatch.setattr(
        downloader,
        "_transcode_video",
        lambda *args: (_ for _ in ()).throw(AssertionError("should not transcode")),
    )

    assert downloader._ensure_sendable(source, metadata, DownloadVariant("video", "medium"), None) == source


def test_ensure_sendable_transcodes_incompatible_video_with_actual_duration(tmp_path, monkeypatch) -> None:
    downloader = VideoDownloader(download_dir=tmp_path, max_upload_bytes=1024)
    source = tmp_path / "video.webm"
    source.write_bytes(b"video")
    metadata = VideoMetadata(
        video_id="id", title=None, description=None, source_url="https://example.com/video",
        webpage_url="https://example.com/video", uploader=None, extractor="Example",
        duration=60, estimated_size_bytes=None,
    )
    calls: list[float | None] = []
    monkeypatch.setattr(
        "bot.downloader._probe_media_streams",
        lambda path: MediaStreams(video_codec="vp9", audio_codec="opus", duration_seconds=5),
    )
    monkeypatch.setattr("bot.downloader._audio_is_silent", lambda path: False)

    def fake_transcode(input_path, output_path, duration):
        calls.append(duration)
        output_path.write_bytes(b"telegram")

    monkeypatch.setattr(downloader, "_transcode_video", fake_transcode)

    result = downloader._ensure_sendable(source, metadata, DownloadVariant("video", "medium"), None)

    assert result == tmp_path / "video.telegram.mp4"
    assert calls == [5]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_audio_silence_detection_distinguishes_silence_and_tone(tmp_path) -> None:
    silent = tmp_path / "silent.wav"
    tone = tmp_path / "tone.wav"
    partial = tmp_path / "partial.wav"
    for path, source in ((silent, "anullsrc=r=8000:cl=mono"), (tone, "sine=frequency=440:sample_rate=8000")):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source, "-t", "1", str(path)],
            check=True,
            capture_output=True,
        )

    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
         "-af", "volume=0:enable='lt(t,1)'", "-t", "2", str(partial)],
        check=True, capture_output=True,
    )

    assert _audio_is_silent(silent)
    assert not _audio_is_silent(tone)
    assert not _audio_is_silent(partial)


def test_h264_with_other_audio_is_remuxed_without_recoding_video(tmp_path, monkeypatch) -> None:
    downloader = VideoDownloader(download_dir=tmp_path, max_upload_bytes=1024)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    metadata = VideoMetadata(
        video_id="id", title=None, description=None, source_url="https://example.com/video",
        webpage_url="https://example.com/video", uploader=None, extractor="Example",
        duration=10, estimated_size_bytes=None,
    )
    monkeypatch.setattr("bot.downloader._probe_media_streams", lambda path: MediaStreams("h264", "opus", 10))
    monkeypatch.setattr("bot.downloader._audio_is_silent", lambda path: False)
    calls = []

    def remux(input_path, output_path, *, copy_audio):
        calls.append(copy_audio)
        output_path.write_bytes(b"result")

    monkeypatch.setattr(downloader, "_remux_video", remux)
    result = downloader._ensure_sendable(source, metadata, DownloadVariant("video", "medium"), None)

    assert result == tmp_path / "video.telegram.mp4"
    assert calls == [False]


def test_remux_copies_video_and_converts_only_audio(tmp_path, monkeypatch) -> None:
    downloader = VideoDownloader(download_dir=tmp_path, max_upload_bytes=1024)
    commands = []
    monkeypatch.setattr("bot.downloader.subprocess.run", lambda command, **kwargs: commands.append(command))

    downloader._remux_video(tmp_path / "input.mp4", tmp_path / "output.mp4", copy_audio=False)

    command = commands[0]
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "aac"


def test_failed_audio_analysis_reports_error_instead_of_assuming_sound(tmp_path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr("bot.downloader.subprocess.run", fail)
    with pytest.raises(DownloadError, match="comprobar el audio"):
        _audio_is_silent(tmp_path / "video.mp4")


def test_missing_audio_retries_alternate_and_cleans_both_directories(tmp_path, monkeypatch) -> None:
    downloader = VideoDownloader(download_dir=tmp_path, max_upload_bytes=1024)
    metadata = VideoMetadata(
        video_id="id", title=None, description=None, source_url="https://example.com/video",
        webpage_url="https://example.com/video", uploader=None, extractor="Example",
        duration=10, estimated_size_bytes=None,
    )
    selectors = []

    def fake_download(url, task_dir, variant, progress, trim_range, **kwargs):
        selectors.append(kwargs.get("format_selector"))
        output = task_dir / "video.mp4"
        output.write_bytes(b"video")
        return output

    def fake_ensure(path, metadata, variant, progress):
        if len(selectors) == 1:
            raise AudioUnavailableError("missing")
        return path

    monkeypatch.setattr(downloader, "_download", fake_download)
    monkeypatch.setattr(downloader, "_ensure_sendable", fake_ensure)
    result = downloader._fetch_with_metadata_sync(metadata, DownloadVariant("video", "medium"))
    task_dir = result.file_path.parent.parent

    assert selectors[0] is None
    assert "bestaudio" in selectors[1]
    downloader.cleanup_result(result)
    assert not task_dir.exists()


def test_timed_out_video_work_cleanup_removes_completed_result(tmp_path) -> None:
    downloader = VideoDownloader(download_dir=tmp_path, max_upload_bytes=1024)
    metadata = VideoMetadata(
        video_id="id", title=None, description=None, source_url="https://example.com/video",
        webpage_url="https://example.com/video", uploader=None, extractor="Example",
        duration=10, estimated_size_bytes=None,
    )
    task_dir = tmp_path / "video-late"
    task_dir.mkdir()
    output = task_dir / "video.mp4"
    output.write_bytes(b"video")

    class CompletedTask:
        def result(self) -> VideoResult:
            return VideoResult(output, metadata, DownloadVariant("video", "medium"))

    downloader._cleanup_timed_out_video_task(CompletedTask())  # type: ignore[arg-type]
    assert not task_dir.exists()


def test_dedicated_cookie_files_are_selected_by_platform_url(tmp_path) -> None:
    generic = tmp_path / "cookies.txt"
    x_cookies = tmp_path / "cookies-x.txt"
    instagram_cookies = tmp_path / "cookies-instagram.txt"
    for path in (generic, x_cookies, instagram_cookies):
        path.write_text("# Netscape HTTP Cookie File\n")
    downloader = VideoDownloader(
        download_dir=tmp_path,
        max_upload_bytes=1024,
        cookies_file=generic,
        x_cookies_file=x_cookies,
        instagram_cookies_file=instagram_cookies,
    )

    assert downloader._base_ydl_opts("https://mobile.x.com/user/status/1")["cookiefile"] == str(x_cookies)
    assert downloader._base_ydl_opts("https://www.twitter.com/user/status/1")["cookiefile"] == str(x_cookies)
    assert downloader._base_ydl_opts("https://www.instagram.com/reel/abc/")["cookiefile"] == str(instagram_cookies)
    assert downloader._base_ydl_opts("https://example.com/video")["cookiefile"] == str(generic)


def test_generic_cookie_file_is_used_when_platform_jar_is_not_configured(tmp_path) -> None:
    generic = tmp_path / "cookies.txt"
    generic.write_text("# Netscape HTTP Cookie File\n")
    downloader = VideoDownloader(
        download_dir=tmp_path,
        max_upload_bytes=1024,
        cookies_file=generic,
    )

    assert downloader._base_ydl_opts("https://x.com/user/status/1")["cookiefile"] == str(generic)
    assert downloader._base_ydl_opts("https://instagram.com/p/abc/")["cookiefile"] == str(generic)


def test_invalid_dedicated_cookie_file_does_not_fall_back_to_generic(tmp_path) -> None:
    generic = tmp_path / "cookies.txt"
    generic.write_text("# Netscape HTTP Cookie File\n")
    downloader = VideoDownloader(
        download_dir=tmp_path,
        max_upload_bytes=1024,
        cookies_file=generic,
        x_cookies_file=tmp_path / "missing-x-cookies.txt",
    )

    try:
        downloader._base_ydl_opts("https://x.com/user/status/1")
    except DownloadError as exc:
        assert str(exc) == (
            "La sesion configurada para X no esta disponible. "
            "Revisa o renueva YTDLP_X_COOKIES_FILE."
        )
    else:
        raise AssertionError("An unavailable dedicated cookie file should fail")


def test_metadata_and_video_download_select_cookies_for_the_source_url(tmp_path, monkeypatch) -> None:
    downloader = VideoDownloader(download_dir=tmp_path, max_upload_bytes=1024)
    source_url = "https://x.com/user/status/1"
    selected_urls: list[str | None] = []
    downloaded = tmp_path / "video.mp4"

    def fake_base_opts(url: str | None = None) -> dict[str, object]:
        selected_urls.append(url)
        return {}

    class FakeYdl:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url: str, download: bool):
            if download:
                downloaded.write_bytes(b"video")
            return {
                "id": "id",
                "title": "title",
                "webpage_url": url,
                "formats": [],
            }

        def prepare_filename(self, info: object) -> str:
            return str(downloaded)

    monkeypatch.setattr(downloader, "_base_ydl_opts", fake_base_opts)
    monkeypatch.setattr("bot.downloader._youtube_dl", lambda opts: FakeYdl())

    downloader._extract_metadata(source_url)
    downloader._download(source_url, tmp_path, DownloadVariant("video", "low"), None)

    assert selected_urls == [source_url, source_url]


def test_image_download_uses_the_platform_selected_cookie_file(tmp_path, monkeypatch) -> None:
    instagram_cookies = tmp_path / "cookies-instagram.txt"
    instagram_cookies.write_text("# Netscape HTTP Cookie File\n")
    downloader = VideoDownloader(
        download_dir=tmp_path,
        max_upload_bytes=1024,
        instagram_cookies_file=instagram_cookies,
    )
    metadata = VideoMetadata(
        video_id="id",
        title="title",
        description=None,
        source_url="https://www.instagram.com/p/abc/",
        webpage_url="https://www.instagram.com/p/abc/",
        uploader=None,
        extractor="Instagram",
        duration=None,
        estimated_size_bytes=None,
        is_video=False,
        is_image=True,
        image_count=1,
        image_urls=["https://cdn.example.com/image.jpg"],
    )
    selected_cookie_files: list[Path | None] = []

    def fake_download_images(
        image_urls,
        count,
        task_dir,
        quality,
        max_size_bytes,
        *,
        cookie_file=None,
    ):
        selected_cookie_files.append(cookie_file)
        return []

    monkeypatch.setattr("bot.downloader._download_images", fake_download_images)
    monkeypatch.setattr(downloader, "_ensure_sendable_images", lambda paths, variant, progress: paths)

    downloader._fetch_images_with_metadata_sync(metadata, ImageVariant("medium"))

    assert selected_cookie_files == [instagram_cookies]


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


def test_apply_image_quality_converts_rgba_png_to_jpeg(tmp_path) -> None:
    from PIL import Image

    source = tmp_path / "source.png"
    Image.new("RGBA", (16, 16), (255, 0, 0, 127)).save(source)

    result = _apply_image_quality(source, "medium", 1024 * 1024)

    assert result.suffix == ".jpg"
    with Image.open(result) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"


def test_download_images_continues_when_one_image_fails(tmp_path, monkeypatch) -> None:
    from PIL import Image

    def fake_download(url: str, save_path) -> None:
        if "bad" in url:
            from bot.downloader import DownloadError

            raise DownloadError("bad")
        Image.new("RGB", (8, 8), (0, 255, 0)).save(save_path, "JPEG")

    monkeypatch.setattr("bot.downloader._download_image_from_url", fake_download)

    result = _download_images(
        ["https://example.com/ok.jpg", "https://example.com/bad.jpg"],
        -1,
        tmp_path,
        "medium",
        1024 * 1024,
    )

    assert len(result) == 1


def test_classify_youtube_bot_verification_error() -> None:
    message = "Sign in to confirm you’re not a bot. Use --cookies"

    assert "YTDLP_COOKIES_FILE" in _classify_yt_dlp_error(message)


def test_classify_x_and_instagram_login_errors_request_dedicated_session_refresh() -> None:
    assert _classify_yt_dlp_error("not authorized", "https://x.com/user/status/1") == (
        "X requiere una sesion valida para este enlace. Renueva YTDLP_X_COOKIES_FILE."
    )
    assert _classify_yt_dlp_error("login required", "https://instagram.com/reel/abc/") == (
        "Instagram requiere una sesion valida para este enlace. "
        "Renueva YTDLP_INSTAGRAM_COOKIES_FILE."
    )


def test_classify_non_authentication_x_error_does_not_request_cookie_refresh() -> None:
    assert _classify_yt_dlp_error("Tweet has been deleted", "https://x.com/user/status/1") == (
        "No he podido descargar ese enlace."
    )


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
