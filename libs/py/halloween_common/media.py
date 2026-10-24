from urllib.parse import quote


def resolve_media_url(url: str, base_url: str) -> str:
    """Resolve a media reference against the site-wide MEDIA_BASE_URL.

    Full http(s) URLs are returned unchanged. Anything else is treated as a
    file name (or path) on the media server; existing %-escapes are preserved.
    """
    if url.lower().startswith(("http://", "https://")):
        return url
    if not base_url:
        raise ValueError(
            f"Media '{url}' is not a full URL and MEDIA_BASE_URL is not set (see config/site.env)"
        )
    return f"{base_url.rstrip('/')}/{quote(url.lstrip('/'), safe='/%')}"
