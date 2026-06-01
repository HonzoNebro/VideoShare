from __future__ import annotations

from dataclasses import dataclass
import asyncio
import hashlib
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Callable, cast

import yt_dlp
from yt_dlp.utils import DownloadError as YtDlpDownloadError

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[str, int], None]
VariantKind = str
VariantQuality = str


class DownloadError(RuntimeError):
    """Raised when a URL cannot be downloaded or processed."""


class OversizeError(DownloadError):
    """Raised when no sendable file can be produced."""


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

    @property
    def cache_key(self) -> str:
        if self.extractor and self.video_id:
            return f"{self.extractor}:{self.video_id}".lower()

        canonical = self.webpage_url or self.source_url
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"url:{digest}"


@dataclass(frozen=True)
class VideoResult:
    file_path: Path
    metadata: VideoMetadata
    variant: DownloadVariant

    @property
    def size_bytes(self) -> int:
        return self.file_path.stat().st_size


def _metadata_from_info(info: dict[str, Any], fallback_url: str) -> VideoMetadata:
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
    )


def variant_cache_key(metadata: VideoMetadata, variant: DownloadVariant) -> str:
    return f"{metadata.cache_key}:{variant.cache_suffix}"


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


class VideoDownloader:
    def __init__(
        self,
        *,
        download_dir: Path,
        max_upload_bytes: int,
        timeout_seconds: int = 600,
        metadata_timeout_seconds: int | None = None,
        cookies_file: Path | None = None,
        js_runtimes: str | None = None,
        remote_components: str | None = None,
        max_video_height: int = 480,
    ) -> None:
        self.download_dir = download_dir
        self.max_upload_bytes = max_upload_bytes
        self.timeout_seconds = timeout_seconds
        self.metadata_timeout_seconds = metadata_timeout_seconds or timeout_seconds
        self.cookies_file = cookies_file
        self.js_runtimes = js_runtimes
        self.remote_components = remote_components
        self.max_video_height = max_video_height

    async def fetch(
        self,
        url: str,
        *,
        variant: DownloadVariant | None = None,
        progress: ProgressCallback | None = None,
    ) -> VideoResult:
        metadata = await self.extract_metadata(url, progress=progress)
        return await self.fetch_with_metadata(metadata, variant=variant, progress=progress)

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
        progress: ProgressCallback | None = None,
    ) -> VideoResult:
        selected_variant = variant or DownloadVariant("video", "medium")
        return await asyncio.wait_for(
            asyncio.to_thread(self._fetch_with_metadata_sync, metadata, selected_variant, progress),
            timeout=self.timeout_seconds,
        )

    def _extract_metadata_sync(
        self,
        url: str,
        progress: ProgressCallback | None = None,
    ) -> VideoMetadata:
        _report(progress, "Obteniendo informacion", 15)
        try:
            return self._extract_metadata(url)
        except YtDlpDownloadError as exc:
            raise DownloadError(_classify_yt_dlp_error(str(exc))) from exc

    def _fetch_with_metadata_sync(
        self,
        metadata: VideoMetadata,
        variant: DownloadVariant,
        progress: ProgressCallback | None = None,
    ) -> VideoResult:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        task_dir = Path(tempfile.mkdtemp(prefix="video-", dir=self.download_dir))

        try:
            _report(progress, _download_label(variant), 25)
            downloaded = self._download(metadata.source_url, task_dir, variant, progress)
            sendable = self._ensure_sendable(downloaded, metadata, variant, progress)
            return VideoResult(file_path=sendable, metadata=metadata, variant=variant)
        except YtDlpDownloadError as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise DownloadError(_classify_yt_dlp_error(str(exc))) from exc
        except subprocess.CalledProcessError as exc:
            LOGGER.warning("ffmpeg failed: %s", exc)
            raise DownloadError("No he podido convertir el video a un formato compatible.") from exc
        except TimeoutError as exc:
            raise DownloadError("La descarga ha tardado demasiado y se ha cancelado.") from exc
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise

    def cleanup_result(self, result: VideoResult) -> None:
        task_dir = result.file_path.parent
        if task_dir.is_relative_to(self.download_dir):
            shutil.rmtree(task_dir, ignore_errors=True)

    def _extract_metadata(self, url: str) -> VideoMetadata:
        opts: dict[str, Any] = self._base_ydl_opts()
        opts.update({
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        })
        with _youtube_dl(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return _metadata_from_info(cast(dict[str, Any], info), url)

    def _download(
        self,
        url: str,
        task_dir: Path,
        variant: DownloadVariant,
        progress: ProgressCallback | None,
    ) -> Path:
        output_template = str(task_dir / "%(title).180B-%(id)s.%(ext)s")
        opts: dict[str, Any] = self._base_ydl_opts()
        opts.update({
            "format": _format_selector(variant, self.max_video_height),
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
        if variant.kind == "audio":
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": _audio_quality_kbps(variant),
            }]
        with _youtube_dl(opts) as ydl:
            info = cast(dict[str, Any], ydl.extract_info(url, download=True))
            filename = _prepare_filename(ydl, info)

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

        prepared = Path(filename)
        if prepared.exists():
            return prepared

        if candidates:
            return candidates[0]

        raise DownloadError("No he encontrado el archivo descargado.")

    def _ensure_sendable(
        self,
        file_path: Path,
        metadata: VideoMetadata,
        variant: DownloadVariant,
        progress: ProgressCallback | None,
    ) -> Path:
        suffix = file_path.suffix.lower()
        if variant.kind == "audio" and suffix in {".m4a", ".mp3"} and file_path.stat().st_size <= self.max_upload_bytes:
            _report(progress, "Preparando envio", 80)
            return file_path

        if variant.kind == "video" and suffix == ".mp4" and file_path.stat().st_size <= self.max_upload_bytes:
            _report(progress, "Preparando envio", 80)
            return file_path

        normalized = file_path.with_suffix(".telegram.m4a" if variant.kind == "audio" else ".telegram.mp4")
        _report(progress, "Reduciendo calidad", 70)
        if variant.kind == "audio":
            self._transcode_audio(file_path, normalized, variant)
        else:
            self._transcode_video(file_path, normalized, metadata.duration)
        if normalized.stat().st_size <= self.max_upload_bytes:
            _report(progress, "Preparando envio", 80)
            return normalized

        raise OversizeError(_oversize_after_transcode_message(variant))

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

    def _base_ydl_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "logger": _YtDlpQuietLogger(),
            "no_color": True,
        }
        if self.cookies_file:
            opts["cookiefile"] = str(self.cookies_file)
        if self.js_runtimes:
            opts["js_runtimes"] = _parse_js_runtimes(self.js_runtimes)
        if self.remote_components:
            opts["remote_components"] = _parse_remote_components(self.remote_components)
        return opts


def _classify_yt_dlp_error(message: str) -> str:
    lower = message.lower()
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
