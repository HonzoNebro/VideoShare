from __future__ import annotations

import re
from html import escape
from urllib.parse import urlparse


URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
CAPTION_LIMIT = 1024


def extract_urls(text: str | None) -> list[str]:
    if not text:
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.findall(text):
        cleaned = match.rstrip(".,;:!?)]}")
        parsed = urlparse(cleaned)
        if parsed.scheme in {"http", "https"} and parsed.netloc and cleaned not in seen:
            urls.append(cleaned)
            seen.add(cleaned)
    return urls


def text_mentions_bot(text: str | None, bot_username: str | None) -> bool:
    if not text or not bot_username:
        return False

    username = bot_username.lstrip("@")
    return re.search(rf"@{re.escape(username)}\b", text, flags=re.IGNORECASE) is not None


def truncate_text(text: str | None, limit: int) -> str:
    if not text:
        return ""

    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    if limit <= 1:
        return "..."
    return normalized[: limit - 3].rstrip() + "..."


def build_caption(
    *,
    title: str | None,
    description: str | None,
    source_url: str,
    uploader: str | None = None,
    max_description_chars: int = 650,
) -> str:
    lines: list[str] = []
    if title:
        lines.append(f"<b>{escape(truncate_text(title, 160))}</b>")
    if uploader:
        lines.append(escape(truncate_text(uploader, 80)))

    short_description = truncate_text(description, max_description_chars)
    if short_description:
        lines.append("")
        lines.append(escape(short_description))

    lines.append("")
    lines.append(f'<a href="{escape(source_url, quote=True)}">Enlace original</a>')

    caption = "\n".join(lines).strip()
    if len(caption) <= CAPTION_LIMIT:
        return caption

    reserved = len(f'\n\n<a href="{escape(source_url, quote=True)}">Enlace original</a>')
    compact_description_limit = max(0, CAPTION_LIMIT - reserved - 260)
    return build_caption(
        title=title,
        description=description,
        source_url=source_url,
        uploader=uploader,
        max_description_chars=compact_description_limit,
    )
