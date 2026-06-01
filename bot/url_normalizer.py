from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "igsh",
    "igshid",
    "mc_cid",
    "mc_eid",
    "si",
    "spm",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = _normalize_path(parsed.path)

    youtube_id = _youtube_video_id(netloc, path, parsed.query)
    if youtube_id:
        return f"https://www.youtube.com/watch?v={youtube_id}"

    query = _normalize_query(parsed.query)
    return urlunparse((scheme, netloc, path, "", query, ""))


def _normalize_path(path: str) -> str:
    if path != "/" and path.endswith("/"):
        return path.rstrip("/")
    return path or ""


def _normalize_query(query: str) -> str:
    params = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    params.sort()
    return urlencode(params, doseq=True)


def _youtube_video_id(netloc: str, path: str, query: str) -> str | None:
    host = netloc.removeprefix("www.")
    clean_path = path.strip("/")

    if host == "youtu.be" and clean_path:
        return clean_path.split("/")[0]

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        parts = clean_path.split("/")
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"} and parts[1]:
            return parts[1]

        if clean_path == "watch":
            for key, value in parse_qsl(query, keep_blank_values=False):
                if key == "v" and value:
                    return value

    return None
