from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import hashlib
from http.cookiejar import MozillaCookieJar
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, cast
from urllib.parse import urlparse
import urllib.request

import yt_dlp
from yt_dlp.utils import DownloadError as YtDlpDownloadError
from PIL import Image

from bot.url_normalizer import reddit_extraction_url

try:
    from yt_dlp.utils import download_range_func
except ImportError:  # pragma: no cover - depends on yt-dlp version
    download_range_func = None

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[str, int], None]
VariantKind = str
VariantQuality = str
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024


class DownloadError(RuntimeError):
    """Raised when a URL cannot be downloaded or processed."""


class OversizeError(DownloadError):
    """Raised when no sendable file can be produced."""


class AudioUnavailableError(DownloadError):
    """Raised when the downloaded audio is missing or effectively silent."""


@dataclass(frozen=True)
class MediaStreams:
    video_codec: str | None
    audio_codec: str | None
    duration_seconds: float | None


@dataclass(frozen=True)
class DownloadVariant:
    kind: VariantKind
    quality: VariantQuality

    def __post_init__(self) -> None:
        if self.kind not in {"video", "audio"}:
            raise ValueError(f"Unsupported download kind: {self.kind}")
        if self.quality not in {"high", "medium", "low"}:
            raise ValueError(f"Unsupported download quality: {self.quality}")

    @property
    def cache_suffix(self) -> str:
        return f"{self.kind}-{self.quality}"


@dataclass(frozen=True)
class ImageVariant:
    quality: VariantQuality
    count: int = 1

    def __post_init__(self) -> None:
        if self.quality not in {"high", "medium", "low"}:
            raise ValueError(f"Unsupported image quality: {self.quality}")
        if self.count not in {1, -1}:
            raise ValueError(f"Unsupported image count: {self.count}")

    @property
    def cache_suffix(self) -> str:
        count_str = "all" if self.count == -1 else str(self.count)
        return f"image-{self.quality}-count{count_str}"


@dataclass(frozen=True)
class TrimRange:
    start_seconds: float
    end_seconds: float

    def __post_init__(self) -> None:
        if self.start_seconds < 0 or self.end_seconds <= self.start_seconds:
            raise ValueError("Invalid trim range")

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def start_centiseconds(self) -> int:
        return int(round(self.start_seconds * 100))

    @property
    def end_centiseconds(self) -> int:
        return int(round(self.end_seconds * 100))

    @property
    def cache_suffix(self) -> str:
        return f"clip-{self.start_centiseconds}-{self.end_centiseconds}"

    @property
    def normalized_text(self) -> str:
        return f"{_format_centiseconds(self.start_centiseconds)}-{_format_centiseconds(self.end_centiseconds)}"


@dataclass(frozen=True)
class VideoMetadata:
    video_id: str | None
    title: str | None
    description: str | None
    source_url: str
    webpage_url: str
    uploader: str | None
    extractor: str | None
    duration: float | None
    estimated_size_bytes: int | None
    is_video: bool = True
    is_image: bool = False
    image_count: int = 0
    image_urls: list[str] = field(default_factory=list)

    @property
    def cache_key(self) -> str:
        if self.extractor and self.video_id:
            return f"{self.extractor}:{self.video_id}".lower()

        canonical = self.webpage_url or self.source_url
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"url:{digest}"

    @property
    def content_type(self) -> str:
        """Returns 'video', 'image', or 'mixed'."""
        if self.is_video and self.is_image:
            return "mixed"
        if self.is_image:
            return "image"
        return "video"


@dataclass(frozen=True)
class VideoResult:
    file_path: Path
    metadata: VideoMetadata
    variant: DownloadVariant
    trim_range: TrimRange | None = None

    @property
    def size_bytes(self) -> int:
        return self.file_path.stat().st_size


@dataclass(frozen=True)
class ImageResult:
    file_paths: list[Path]
    metadata: VideoMetadata
    variant: ImageVariant

    @property
    def size_bytes(self) -> int:
        """Total size of all images."""
        return sum(path.stat().st_size for path in self.file_paths)

    @property
    def count(self) -> int:
        """Number of images."""
        return len(self.file_paths)


def _metadata_from_info(info: dict[str, Any], fallback_url: str) -> VideoMetadata:
    is_video, is_image, image_count, image_urls = _detect_content_type(info)

    return VideoMetadata(
        video_id=info.get("id"),
        title=info.get("title"),
        description=info.get("description"),
        source_url=fallback_url,
        webpage_url=info.get("webpage_url") or fallback_url,
        uploader=info.get("uploader") or info.get("channel"),
        extractor=info.get("extractor_key") or info.get("extractor"),
        duration=info.get("duration"),
        estimated_size_bytes=_estimated_size_bytes(info),
        is_video=is_video,
        is_image=is_image,
        image_count=image_count,
        image_urls=image_urls,
    )


def _detect_content_type(info: dict[str, Any]) -> tuple[bool, bool, int, list[str]]:
    image_urls: list[str] = []
    entries = [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
    for entry in entries:
        image_url = _image_url_from_info(entry, allow_thumbnail_fallback=True)
        if image_url:
            image_urls.append(image_url)

    direct_image_url = _image_url_from_info(info, allow_thumbnail_fallback=not _has_video(info))
    if direct_image_url:
        image_urls.insert(0, direct_image_url)

    image_urls = _dedupe_preserving_order(image_urls)
    has_video = _has_video(info) or any(_has_video(entry) for entry in entries)
    is_image = bool(image_urls)
    is_video = has_video or not is_image
    return is_video, is_image, len(image_urls), image_urls


def _has_video(info: dict[str, Any]) -> bool:
    if info.get("duration"):
        return True
    formats = info.get("formats") or []
    for item in formats:
        if not isinstance(item, dict):
            continue
        vcodec = item.get("vcodec")
        if vcodec and vcodec != "none":
            return True
        if item.get("height") and item.get("ext") not in _IMAGE_EXTENSIONS:
            return True
    ext = str(info.get("ext") or "").lower()
    return bool(info.get("url") and ext and ext not in _IMAGE_EXTENSIONS)


_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "heic", "heif"}


def _image_url_from_info(info: dict[str, Any], *, allow_thumbnail_fallback: bool) -> str | None:
    direct_url = info.get("url")
    ext = str(info.get("ext") or "").lower()
    if isinstance(direct_url, str) and (ext in _IMAGE_EXTENSIONS or info.get("vcodec") == "none"):
        return direct_url

    for key in ("image", "image_url", "display_url", "thumbnail"):
        value = info.get(key)
        if isinstance(value, str) and _looks_like_image_url(value):
            return value

    if allow_thumbnail_fallback:
        thumbnails = [item for item in info.get("thumbnails") or [] if isinstance(item, dict)]
        if thumbnails:
            thumbnail = max(thumbnails, key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0))
            url = thumbnail.get("url")
            if isinstance(url, str):
                return url

    return None


def _looks_like_image_url(url: str) -> bool:
    lower = url.lower().split("?", 1)[0]
    return any(lower.endswith(f".{ext}") for ext in _IMAGE_EXTENSIONS)


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def variant_cache_key(
    metadata: VideoMetadata,
    variant: DownloadVariant | ImageVariant,
    trim_range: TrimRange | None = None,
) -> str:
    key = f"{metadata.cache_key}:{variant.cache_suffix}"
    if trim_range:
        key = f"{key}:{trim_range.cache_suffix}"
    if isinstance(variant, DownloadVariant):
        key = f"{key}:audio-validated-v1"
    return key


TRIM_RANGE_PATTERN = r"(\d+):([0-5]\d)\.(\d{1,2})\s*-\s*(\d+):([0-5]\d)\.(\d{1,2})"
TRIM_RANGE_RE = re.compile(rf"^\s*{TRIM_RANGE_PATTERN}\s*$")
TRIM_RANGE_SEARCH_RE = re.compile(TRIM_RANGE_PATTERN)


def parse_trim_range(raw: str) -> TrimRange:
    match = TRIM_RANGE_RE.match(raw) or TRIM_RANGE_SEARCH_RE.search(raw)
    if not match:
        raise ValueError("Formato de recorte no valido.")

    start_minutes = int(match.group(1))
    start_seconds = int(match.group(2))
    start_centis = _parse_centiseconds(match.group(3))
    end_minutes = int(match.group(4))
    end_seconds = int(match.group(5))
    end_centis = _parse_centiseconds(match.group(6))
    start = start_minutes * 60 + start_seconds + start_centis / 100
    end = end_minutes * 60 + end_seconds + end_centis / 100
    if end <= start:
        raise ValueError("El fin del recorte debe ser posterior al inicio.")
    return TrimRange(start_seconds=start, end_seconds=end)


def calculate_video_bitrate_kbps(
    *,
    duration_seconds: float | None,
    target_bytes: int,
    audio_kbps: int = 96,
    safety_ratio: float = 0.88,
    minimum_video_kbps: int = 180,
) -> int | None:
    if not duration_seconds or duration_seconds <= 0:
        return None

    total_kbps = int((target_bytes * 8 / duration_seconds / 1000) * safety_ratio)
    video_kbps = total_kbps - audio_kbps
    if video_kbps < minimum_video_kbps:
        return None
    return video_kbps


def _probe_media_streams(file_path: Path) -> MediaStreams:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name:format=duration",
        "-of",
        "json",
        str(file_path),
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        info = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise DownloadError("No he podido comprobar los streams del video descargado.") from exc

    streams = info.get("streams") if isinstance(info, dict) else None
    video_codec: str | None = None
    audio_codec: str | None = None
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            codec_type = stream.get("codec_type")
            codec_name = stream.get("codec_name")
            if not isinstance(codec_name, str):
                continue
            if codec_type == "video" and video_codec is None:
                video_codec = codec_name.lower()
            elif codec_type == "audio" and audio_codec is None:
                audio_codec = codec_name.lower()

    duration_seconds: float | None = None
    media_format = info.get("format") if isinstance(info, dict) else None
    if isinstance(media_format, dict):
        try:
            duration_seconds = float(media_format["duration"])
        except (KeyError, TypeError, ValueError):
            pass
    return MediaStreams(video_codec, audio_codec, duration_seconds)


def _audio_is_silent(file_path: Path) -> bool:
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-i", str(file_path),
        "-map", "0:a:0", "-vn", "-af", "volumedetect", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        LOGGER.warning("Could not analyze downloaded audio: %s", type(exc).__name__)
        raise DownloadError("No he podido comprobar el audio del archivo descargado.") from exc
    match = re.search(r"max_volume:\s*(-?inf|-?\d+(?:\.\d+)?)\s*dB", result.stderr)
    if not match:
        LOGGER.warning("Could not determine downloaded audio volume")
        raise DownloadError("No he podido comprobar el audio del archivo descargado.")
    return match.group(1) == "-inf" or float(match.group(1)) <= -60


def _format_centiseconds(total_centiseconds: int) -> str:
    minutes, remainder = divmod(total_centiseconds, 60 * 100)
    seconds, centiseconds = divmod(remainder, 100)
    return f"{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _parse_centiseconds(raw: str) -> int:
    return int(raw) * 10 if len(raw) == 1 else int(raw)


class VideoDownloader:
    def __init__(
        self,
        *,
        download_dir: Path,
        max_upload_bytes: int,
        timeout_seconds: int = 600,
        metadata_timeout_seconds: int | None = None,
        cookies_file: Path | None = None,
        x_cookies_file: Path | None = None,
        instagram_cookies_file: Path | None = None,
        js_runtimes: str | None = None,
        remote_components: str | None = None,
        max_video_height: int = 480,
    ) -> None:
        self.download_dir = download_dir
        self.max_upload_bytes = max_upload_bytes
        self.timeout_seconds = timeout_seconds
        self.metadata_timeout_seconds = metadata_timeout_seconds or timeout_seconds
        self.cookies_file = cookies_file
        self.x_cookies_file = x_cookies_file
        self.instagram_cookies_file = instagram_cookies_file
        self.js_runtimes = js_runtimes
        self.remote_components = remote_components
        self.max_video_height = max_video_height

    async def fetch(
        self,
        url: str,
        *,
        variant: DownloadVariant | None = None,
        trim_range: TrimRange | None = None,
        progress: ProgressCallback | None = None,
    ) -> VideoResult:
        metadata = await self.extract_metadata(url, progress=progress)
        return await self.fetch_with_metadata(
            metadata,
            variant=variant,
            trim_range=trim_range,
            progress=progress,
        )

    async def extract_metadata(
        self,
        url: str,
        progress: ProgressCallback | None = None,
    ) -> VideoMetadata:
        return await asyncio.wait_for(
            asyncio.to_thread(self._extract_metadata_sync, url, progress),
            timeout=self.metadata_timeout_seconds,
        )

    async def fetch_with_metadata(
        self,
        metadata: VideoMetadata,
        *,
        variant: DownloadVariant | None = None,
        trim_range: TrimRange | None = None,
        progress: ProgressCallback | None = None,
    ) -> VideoResult:
        selected_variant = variant or DownloadVariant("video", "medium")
        work = asyncio.create_task(asyncio.to_thread(
            self._fetch_with_metadata_sync,
            metadata,
            selected_variant,
            trim_range,
            progress,
        ))
        try:
            return await asyncio.wait_for(asyncio.shield(work), timeout=self.timeout_seconds)
        except (TimeoutError, asyncio.CancelledError):
            work.add_done_callback(self._cleanup_timed_out_video_task)
            raise

    def _extract_metadata_sync(
        self,
        url: str,
        progress: ProgressCallback | None = None,
    ) -> VideoMetadata:
        _report(progress, "Obteniendo informacion", 15)
        try:
            return self._extract_metadata(url)
        except YtDlpDownloadError as exc:
            raise DownloadError(_classify_yt_dlp_error(str(exc), url)) from exc

    def _fetch_with_metadata_sync(
        self,
        metadata: VideoMetadata,
        variant: DownloadVariant,
        trim_range: TrimRange | None = None,
        progress: ProgressCallback | None = None,
    ) -> VideoResult:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        task_dir = Path(tempfile.mkdtemp(prefix="video-", dir=self.download_dir))

        try:
            _report(progress, _download_label(variant), 25)
            downloaded = self._download(metadata.source_url, task_dir, variant, progress, trim_range)
            for attempt in range(2):
                prepared = self._trim_download(
                    downloaded,
                    trim_range,
                    variant,
                    progress,
                    section_downloaded=bool(trim_range and download_range_func),
                )
                try:
                    sendable = self._ensure_sendable(prepared, metadata, variant, progress)
                    break
                except AudioUnavailableError:
                    if attempt:
                        raise
                    _report(progress, "Buscando otra pista de audio", 30)
                    alternate_dir = task_dir / "alternate"
                    alternate_dir.mkdir()
                    try:
                        downloaded = self._download(
                            metadata.source_url, alternate_dir, variant, progress, trim_range,
                            format_selector=_alternate_audio_selector(variant),
                        )
                    except YtDlpDownloadError as exc:
                        raise AudioUnavailableError(
                            "No he encontrado una variante con audio audible. "
                            "Puede que el contenido original no tenga sonido."
                        ) from exc
            return VideoResult(
                file_path=sendable,
                metadata=metadata,
                variant=variant,
                trim_range=trim_range,
            )
        except YtDlpDownloadError as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise DownloadError(_classify_yt_dlp_error(str(exc), metadata.source_url)) from exc
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            LOGGER.warning("ffmpeg failed: %s", exc)
            raise DownloadError("No he podido convertir el video a un formato compatible.") from exc
        except TimeoutError as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise DownloadError("La descarga ha tardado demasiado y se ha cancelado.") from exc
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise

    def cleanup_result(self, result: VideoResult) -> None:
        try:
            relative = result.file_path.relative_to(self.download_dir)
        except ValueError:
            return
        if len(relative.parts) >= 2 and relative.parts[0].startswith("video-"):
            shutil.rmtree(self.download_dir / relative.parts[0], ignore_errors=True)

    def _cleanup_timed_out_video_task(self, task: asyncio.Task[VideoResult]) -> None:
        try:
            result = task.result()
        except Exception:
            return
        self.cleanup_result(result)

    def cleanup_image_result(self, result: ImageResult) -> None:
        parents = {path.parent for path in result.file_paths}
        for task_dir in parents:
            if task_dir.is_relative_to(self.download_dir):
                shutil.rmtree(task_dir, ignore_errors=True)

    def _extract_metadata(self, url: str) -> VideoMetadata:
        opts: dict[str, Any] = self._base_ydl_opts(url)
        opts.update({
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        })
        with _youtube_dl(opts) as ydl:
            info = ydl.extract_info(reddit_extraction_url(url), download=False)
        return _metadata_from_info(cast(dict[str, Any], info), url)

    def _download(
        self,
        url: str,
        task_dir: Path,
        variant: DownloadVariant,
        progress: ProgressCallback | None,
        trim_range: TrimRange | None = None,
        *,
        format_selector: str | None = None,
    ) -> Path:
        output_template = str(task_dir / "%(title).180B-%(id)s.%(ext)s")
        opts: dict[str, Any] = self._base_ydl_opts(url)
        opts.update({
            "format": format_selector or _format_selector(variant, self.max_video_height),
            "merge_output_format": "m4a" if variant.kind == "audio" else "mp4",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "restrictfilenames": True,
            "overwrites": True,
            "progress_hooks": [_download_progress_hook(progress)] if progress else [],
        })
        if variant.kind == "video":
            opts["format_sort"] = ["res", "vcodec:h264", "acodec:aac"]
        if trim_range and download_range_func:
            opts["download_ranges"] = download_range_func(
                None,
                [(trim_range.start_seconds, trim_range.end_seconds)],
            )
            opts["force_keyframes_at_cuts"] = True
        if variant.kind == "audio":
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": _audio_quality_kbps(variant),
            }]
        with _youtube_dl(opts) as ydl:
            info = cast(dict[str, Any], ydl.extract_info(reddit_extraction_url(url), download=True))
            filename = _prepare_filename(ydl, info)

        for candidate in (info.get("filepath"), info.get("_filename"), filename):
            if isinstance(candidate, str):
                prepared = Path(candidate)
                if prepared.exists():
                    return prepared

        candidates = sorted(task_dir.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
        if variant.kind == "audio":
            audio_candidates = [
                path for path in candidates if path.suffix.lower() in {".m4a", ".mp3", ".opus"}
            ]
            if audio_candidates:
                return audio_candidates[0]

        mp4_candidates = [path for path in candidates if path.suffix.lower() == ".mp4"]
        if mp4_candidates:
            return mp4_candidates[0]

        if candidates:
            return candidates[0]

        raise DownloadError("No he encontrado el archivo descargado.")

    def _trim_download(
        self,
        file_path: Path,
        trim_range: TrimRange | None,
        variant: DownloadVariant,
        progress: ProgressCallback | None,
        *,
        section_downloaded: bool = False,
    ) -> Path:
        if not trim_range:
            return file_path

        suffix = ".trimmed.m4a" if variant.kind == "audio" else ".trimmed.mp4"
        output_path = file_path.with_suffix(suffix)
        effective_range = (
            TrimRange(start_seconds=0, end_seconds=trim_range.duration_seconds)
            if section_downloaded
            else trim_range
        )
        _report(progress, "Recortando", 68)
        if variant.kind == "audio":
            self._trim_audio(file_path, output_path, effective_range, variant)
        else:
            self._trim_video(file_path, output_path, effective_range)
        return output_path

    def _trim_video(self, input_path: Path, output_path: Path, trim_range: TrimRange) -> None:
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{trim_range.start_seconds:.2f}",
            "-i",
            str(input_path),
            "-t",
            f"{trim_range.duration_seconds:.2f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(command, check=True, capture_output=True)

    def _trim_audio(
        self,
        input_path: Path,
        output_path: Path,
        trim_range: TrimRange,
        variant: DownloadVariant,
    ) -> None:
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{trim_range.start_seconds:.2f}",
            "-i",
            str(input_path),
            "-t",
            f"{trim_range.duration_seconds:.2f}",
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            f"{_audio_quality_kbps(variant)}k",
            str(output_path),
        ]
        subprocess.run(command, check=True, capture_output=True)

    def _ensure_sendable(
        self,
        file_path: Path,
        metadata: VideoMetadata,
        variant: DownloadVariant,
        progress: ProgressCallback | None,
    ) -> Path:
        suffix = file_path.suffix.lower()
        streams = _probe_media_streams(file_path)
        if not streams.audio_codec:
            raise AudioUnavailableError("El archivo descargado no contiene una pista de audio.")
        _report(progress, "Comprobando audio", 69)
        if _audio_is_silent(file_path):
            raise AudioUnavailableError(
                "El audio descargado parece estar en silencio, incluso al probar otra variante. "
                "Puede que el contenido original no tenga sonido."
            )
        if variant.kind == "audio" and suffix in {".m4a", ".mp3"} and file_path.stat().st_size <= self.max_upload_bytes:
            _report(progress, "Preparando envio", 80)
            return file_path

        video_streams: MediaStreams | None = None
        if variant.kind == "video":
            video_streams = streams
            if (
                suffix == ".mp4"
                and file_path.stat().st_size <= self.max_upload_bytes
                and video_streams.video_codec == "h264"
                and video_streams.audio_codec == "aac"
            ):
                _report(progress, "Preparando envio", 80)
                return file_path

            if video_streams.video_codec == "h264" and file_path.stat().st_size <= self.max_upload_bytes:
                normalized = file_path.with_suffix(".telegram.mp4")
                _report(progress, "Preparando audio", 70)
                try:
                    self._remux_video(file_path, normalized, copy_audio=video_streams.audio_codec == "aac")
                except subprocess.CalledProcessError:
                    LOGGER.warning("Could not remux video; falling back to conversion")
                else:
                    if normalized.stat().st_size <= self.max_upload_bytes:
                        _report(progress, "Preparando envio", 80)
                        return normalized

        normalized = file_path.with_suffix(".telegram.m4a" if variant.kind == "audio" else ".telegram.mp4")
        _report(progress, "Reduciendo calidad", 70)
        if variant.kind == "audio":
            self._transcode_audio(file_path, normalized, variant)
        else:
            self._transcode_video(
                file_path,
                normalized,
                video_streams.duration_seconds if video_streams else metadata.duration,
            )
        if normalized.stat().st_size <= self.max_upload_bytes:
            _report(progress, "Preparando envio", 80)
            return normalized

        raise OversizeError(_oversize_after_transcode_message(variant))

    def _remux_video(self, input_path: Path, output_path: Path, *, copy_audio: bool) -> None:
        command = [
            "ffmpeg", "-y", "-i", str(input_path), "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "copy", "-c:a", "copy" if copy_audio else "aac",
        ]
        if not copy_audio:
            command += ["-b:a", "96k"]
        command += ["-movflags", "+faststart", str(output_path)]
        subprocess.run(command, check=True, capture_output=True)

    def _transcode_video(self, input_path: Path, output_path: Path, duration: float | None) -> None:
        bitrate_kbps = calculate_video_bitrate_kbps(
            duration_seconds=duration,
            target_bytes=self.max_upload_bytes,
        )
        if bitrate_kbps is None:
            raise OversizeError("El video es demasiado largo para reducirlo de forma útil.")

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            "scale='min(1280,iw)':-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            f"{bitrate_kbps}k",
            "-maxrate",
            f"{bitrate_kbps}k",
            "-bufsize",
            f"{bitrate_kbps * 2}k",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(command, check=True, capture_output=True)

    def _transcode_audio(
        self,
        input_path: Path,
        output_path: Path,
        variant: DownloadVariant,
    ) -> None:
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            f"{_audio_quality_kbps(variant)}k",
            str(output_path),
        ]
        subprocess.run(command, check=True, capture_output=True)

    def _base_ydl_opts(self, url: str | None = None) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "logger": _YtDlpQuietLogger(),
            "no_color": True,
        }
        if cookie_file := self._cookie_file_for_url(url):
            opts["cookiefile"] = str(cookie_file)
        if self.js_runtimes:
            opts["js_runtimes"] = _parse_js_runtimes(self.js_runtimes)
        if self.remote_components:
            opts["remote_components"] = _parse_remote_components(self.remote_components)
        return opts

    def _cookie_file_for_url(self, url: str | None) -> Path | None:
        dedicated_file, platform, setting_name = _dedicated_cookie_config(
            url,
            self.x_cookies_file,
            self.instagram_cookies_file,
        )
        if dedicated_file is None:
            return self.cookies_file
        if not dedicated_file.is_file() or not os.access(dedicated_file, os.R_OK):
            raise DownloadError(
                f"La sesion configurada para {platform} no esta disponible. "
                f"Revisa o renueva {setting_name}."
            )
        return dedicated_file

    # Image download methods
    async def fetch_images(
        self,
        url: str,
        *,
        variant: ImageVariant | None = None,
        progress: ProgressCallback | None = None,
    ) -> ImageResult:
        """Fetch and download images from URL."""
        metadata = await self.extract_metadata(url, progress=progress)
        return await self.fetch_images_with_metadata(
            metadata,
            variant=variant,
            progress=progress,
        )

    async def fetch_images_with_metadata(
        self,
        metadata: VideoMetadata,
        *,
        variant: ImageVariant | None = None,
        progress: ProgressCallback | None = None,
    ) -> ImageResult:
        """Fetch images with pre-extracted metadata."""
        if not metadata.is_image or not metadata.image_urls:
            raise DownloadError("El enlace no contiene imágenes")

        selected_variant = variant or ImageVariant(quality="medium", count=1)
        work = asyncio.create_task(asyncio.to_thread(
            self._fetch_images_with_metadata_sync,
            metadata,
            selected_variant,
            progress,
        ))
        try:
            return await asyncio.wait_for(asyncio.shield(work), timeout=self.timeout_seconds)
        except (TimeoutError, asyncio.CancelledError):
            work.add_done_callback(self._cleanup_timed_out_image_task)
            raise

    def _fetch_images_with_metadata_sync(
        self,
        metadata: VideoMetadata,
        variant: ImageVariant,
        progress: ProgressCallback | None = None,
    ) -> ImageResult:
        """Synchronous image fetching."""
        self.download_dir.mkdir(parents=True, exist_ok=True)
        task_dir = Path(tempfile.mkdtemp(prefix="images-", dir=self.download_dir))

        try:
            _report(progress, "Descargando imágenes", 25)

            # Download and process images
            downloaded = _download_images(
                metadata.image_urls,
                variant.count,
                task_dir,
                variant.quality,
                self.max_upload_bytes,
                cookie_file=self._cookie_file_for_url(metadata.source_url),
            )

            # Ensure images fit Telegram limits
            sendable = self._ensure_sendable_images(downloaded, variant, progress)

            return ImageResult(
                file_paths=sendable,
                metadata=metadata,
                variant=variant,
            )
        except Exception as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            if isinstance(exc, DownloadError):
                raise
            raise DownloadError(f"Error al descargar imágenes: {str(exc)}") from exc

    def _cleanup_timed_out_image_task(self, task: asyncio.Task[ImageResult]) -> None:
        try:
            result = task.result()
        except Exception:
            return
        self.cleanup_image_result(result)

    def _ensure_sendable_images(
        self,
        image_paths: list[Path],
        variant: ImageVariant,
        progress: ProgressCallback | None = None,
    ) -> list[Path]:
        """Ensure images fit within Telegram's size limit."""
        _report(progress, "Verificando tamaño", 75)
        max_photo_bytes = min(self.max_upload_bytes, TELEGRAM_PHOTO_MAX_BYTES)
        sendable_images: list[Path] = []
        for img_path in image_paths:
            if img_path.stat().st_size <= max_photo_bytes:
                sendable_images.append(img_path)
                continue

            recompressed = _apply_image_quality(img_path, "low", max_photo_bytes)
            if recompressed.stat().st_size > max_photo_bytes:
                raise OversizeError("Una imagen sigue siendo demasiado grande para enviarla como foto.")
            sendable_images.append(recompressed)
            if recompressed != img_path and img_path.exists():
                img_path.unlink()

        _report(progress, "Listo", 95)
        return sendable_images


def _classify_yt_dlp_error(message: str, source_url: str | None = None) -> str:
    lower = message.lower()
    platform = _cookie_platform_for_url(source_url)
    authentication_markers = (
        "authentication",
        "auth_token",
        "cookies",
        "login",
        "not authorized",
        "sign in",
        "unauthorized",
    )
    if platform == "X" and any(marker in lower for marker in authentication_markers):
        return "X requiere una sesion valida para este enlace. Renueva YTDLP_X_COOKIES_FILE."
    if platform == "Instagram" and any(marker in lower for marker in authentication_markers):
        return "Instagram requiere una sesion valida para este enlace. Renueva YTDLP_INSTAGRAM_COOKIES_FILE."

    host = (urlparse(source_url).hostname or "").lower().rstrip(".") if source_url else ""
    if host == "redd.it" or host == "reddit.com" or host.endswith(".reddit.com"):
        if "private subreddit" in lower:
            return "No he podido acceder a esa comunidad privada de Reddit."
        if "unable to access the reddit api" in lower:
            return "Reddit ha bloqueado el acceso a su API desde esta conexion. Intentalo mas tarde."
        if any(marker in lower for marker in authentication_markers + ("quarantined", "age-restricted")):
            return (
                "Reddit puede requerir una sesion valida o limitar el acceso a esta publicacion. "
                "Para contenido publico, revisa YTDLP_COOKIES_FILE y actualiza yt-dlp."
            )

    bot_markers = ("not a bot", "confirm you're not a bot", "confirm you’re not a bot")
    if any(marker in lower for marker in bot_markers):
        return (
            "YouTube ha pedido verificar que no eres un bot. "
            "Configura YTDLP_COOKIES_FILE con cookies exportadas para poder procesar este enlace."
        )

    private_markers = ("private", "login", "sign in", "cookies", "not available")
    if any(marker in lower for marker in private_markers):
        return "No he podido acceder al video. Solo se admiten enlaces publicos."
    if "challenge solving failed" in lower or "only images are available" in lower:
        return (
            "YouTube no ha podido resolver el desafio JavaScript del video. "
            "Instala Deno 2+ y configura YTDLP_JS_RUNTIMES=deno y "
            "YTDLP_REMOTE_COMPONENTS=ejs:npm."
        )
    return "No he podido descargar ese enlace."


def _dedicated_cookie_config(
    url: str | None,
    x_cookies_file: Path | None,
    instagram_cookies_file: Path | None,
) -> tuple[Path | None, str | None, str | None]:
    platform = _cookie_platform_for_url(url)
    if platform == "X":
        return x_cookies_file, platform, "YTDLP_X_COOKIES_FILE"
    if platform == "Instagram":
        return instagram_cookies_file, platform, "YTDLP_INSTAGRAM_COOKIES_FILE"
    return None, None, None


def _cookie_platform_for_url(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host in {"x.com", "twitter.com"} or host.endswith((".x.com", ".twitter.com")):
        return "X"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "Instagram"
    return None


def _report(progress: ProgressCallback | None, label: str, percent: int) -> None:
    if progress:
        progress(label, percent)


def _download_label(variant: DownloadVariant) -> str:
    return "Descargando audio" if variant.kind == "audio" else "Descargando video"


def _format_selector(variant: DownloadVariant, fallback_max_video_height: int) -> str:
    if variant.kind == "audio":
        return "bestaudio/best"

    if variant.quality == "high":
        return (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "best[ext=mp4]/best"
        )

    max_height = 720 if variant.quality == "medium" else 360

    return (
        f"best[height<={max_height}][ext=mp4]/"
        f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={max_height}]/best[height<=360]/worst"
    )


def _alternate_audio_selector(variant: DownloadVariant) -> str:
    if variant.kind == "audio":
        return "bestaudio/best[acodec!=none]"
    height = "" if variant.quality == "high" else f"[height<={720 if variant.quality == 'medium' else 360}]"
    return f"bestvideo{height}+bestaudio/best{height}[acodec!=none]"


def _audio_quality_kbps(variant: DownloadVariant) -> str:
    if variant.quality == "high":
        return "192"
    if variant.quality == "medium":
        return "128"
    return "64"


def _oversize_after_transcode_message(variant: DownloadVariant) -> str:
    if variant.kind == "audio":
        return "El audio sigue siendo demasiado grande para Telegram tras reducir la calidad."
    return "El video sigue siendo demasiado grande para Telegram tras reducir la calidad."


def _youtube_dl(opts: dict[str, Any]) -> Any:
    youtube_dl: Any = yt_dlp.YoutubeDL
    return youtube_dl(opts)


def _parse_js_runtimes(raw: str) -> dict[str, dict[str, Any]]:
    runtimes: dict[str, dict[str, Any]] = {}
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        name, separator, path = value.partition(":")
        config: dict[str, Any] = {}
        if separator and path:
            config["path"] = path
        runtimes[name] = config
    return runtimes


def _parse_remote_components(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


class _YtDlpQuietLogger:
    def debug(self, message: str) -> None:
        LOGGER.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        LOGGER.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        LOGGER.debug("yt-dlp: %s", message)


def _prepare_filename(ydl: Any, info: dict[str, Any]) -> str:
    return str(ydl.prepare_filename(info))


def _estimated_size_bytes(info: dict[str, Any]) -> int | None:
    direct = info.get("filesize") or info.get("filesize_approx")
    if isinstance(direct, int) and direct > 0:
        return direct

    sizes = []
    for item in info.get("formats") or []:
        if not isinstance(item, dict):
            continue
        size = item.get("filesize") or item.get("filesize_approx")
        if isinstance(size, int) and size > 0:
            sizes.append(size)

    return max(sizes) if sizes else None


def _download_progress_hook(progress: ProgressCallback | None) -> Callable[[dict[str, Any]], None]:
    def hook(data: dict[str, Any]) -> None:
        if not progress:
            return

        status = data.get("status")
        if status == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            if total:
                percent = 25 + int(min(1, downloaded / total) * 35)
                progress("Descargando", percent)
        elif status == "finished":
            progress("Procesando descarga", 65)

    return hook


# Image handling functions

def _download_image_from_url(
    url: str,
    save_path: Path,
    cookie_jar: MozillaCookieJar | None = None,
) -> None:
    """Download a single image from URL and save to file."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        opener = (
            urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
            if cookie_jar
            else urllib.request
        )
        with opener.open(request, timeout=60) as response:
            save_path.write_bytes(response.read())
    except Exception as exc:
        raise DownloadError(f"No se pudo descargar imagen: {str(exc)}") from exc


def _apply_image_quality(
    image_path: Path,
    quality: str,
    max_size_bytes: int,
) -> Path:
    """
    Apply quality compression to an image. Returns path to compressed image.

    Quality levels:
    - 'high': JPEG quality 95, no downscaling
    - 'medium': JPEG quality 80, downscale to 80% if needed for size
    - 'low': JPEG quality 60, downscale to 60% if needed for size
    """
    try:
        img = Image.open(image_path)

        # Quality settings
        quality_map = {
            "high": {"quality": 95, "scale": 1.0},
            "medium": {"quality": 80, "scale": 0.8},
            "low": {"quality": 60, "scale": 0.6},
        }
        settings = quality_map.get(quality, quality_map["medium"])

        # Convert RGBA/other formats to RGB for JPEG
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        output_path = image_path.with_name(f"{image_path.stem}_compressed.jpg")
        img.save(output_path, "JPEG", quality=settings["quality"], optimize=True)

        # Check size and downscale if needed
        current_size = output_path.stat().st_size
        if current_size > max_size_bytes:
            # Calculate scale factor needed
            scale_factor = (max_size_bytes / current_size) ** 0.5
            scale_factor = min(scale_factor, settings["scale"])

            if scale_factor < 1.0:
                new_width = max(1, int(img.width * scale_factor))
                new_height = max(1, int(img.height * scale_factor))
                img_scaled = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                img_scaled.save(output_path, "JPEG", quality=settings["quality"], optimize=True)

        return output_path
    except Exception as exc:
        raise DownloadError(f"Error al procesar imagen: {str(exc)}") from exc


def _download_images(
    image_urls: list[str],
    count: int,
    task_dir: Path,
    quality: str,
    max_size_bytes: int,
    *,
    cookie_file: Path | None = None,
) -> list[Path]:
    """
    Download and process multiple images.

    Args:
        image_urls: List of image URLs to download
        count: Number of images to download (-1 for all, N for first N)
        task_dir: Directory to save images
        quality: Quality level ('high', 'medium', 'low')
        max_size_bytes: Max size per image

    Returns:
        List of Path objects to downloaded and processed images
    """
    if not image_urls:
        raise DownloadError("No hay imágenes disponibles para descargar")

    # Determine how many to download
    if count == -1:
        target_count = len(image_urls)
    else:
        target_count = min(count, len(image_urls))

    if target_count <= 0:
        raise DownloadError("Número inválido de imágenes a descargar")

    downloaded_images: list[Path] = []
    cookie_jar = _load_cookie_jar(cookie_file) if cookie_file else None

    for idx, url in enumerate(image_urls[:target_count]):
        try:
            temp_path = task_dir / f"image_{idx}.download"
            if cookie_jar:
                _download_image_from_url(url, temp_path, cookie_jar)
            else:
                _download_image_from_url(url, temp_path)

            # Apply quality compression
            processed_path = _apply_image_quality(temp_path, quality, max_size_bytes)
            downloaded_images.append(processed_path)

            # Clean up temp file if different
            if processed_path != temp_path and temp_path.exists():
                temp_path.unlink()
        except DownloadError:
            # Continue with next image instead of failing completely
            LOGGER.warning(f"No se pudo descargar imagen {idx}: {url}")
            continue

    if not downloaded_images:
        raise DownloadError("No se pudo descargar ninguna imagen")

    return downloaded_images


def _load_cookie_jar(cookie_file: Path) -> MozillaCookieJar:
    cookie_jar = MozillaCookieJar(str(cookie_file))
    try:
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
    except OSError as exc:
        raise DownloadError("No he podido leer las cookies configuradas para descargar imágenes.") from exc
    return cookie_jar
