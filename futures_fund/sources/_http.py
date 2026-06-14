"""Tiny shared HTTP helpers for the source adapters.

Every fetch is fail-soft: a network error, non-2xx, or missing client returns ``None``/``b""``
rather than raising, so an adapter loop can simply skip a dead upstream (mirrors base
``vendors.fetch_news`` semantics).
"""
from __future__ import annotations

from typing import Any

# A browser-ish UA — several keyless endpoints (reddit, nitter mirrors) 403 the default client UA.
DEFAULT_UA = "Mozilla/5.0 (OracleDesk research; keyless public read)"

# HYGIENE: a hard ceiling on the bytes we will buffer from any single UNTRUSTED public feed. A
# hostile/broken upstream returning a multi-GB body must never balloon desk memory — a response
# advertising (Content-Length) or actually exceeding this cap is ABORTED and treated as a failure
# (None), exactly like a network error. 5 MB comfortably covers every real RSS/JSON/HTML feed.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def _oversized_content_length(r: Any, max_bytes: int) -> bool:
    """True when the response ADVERTISES a Content-Length larger than the cap, so we can abort
    BEFORE reading the body. Tolerant: a missing/garbage header is not 'oversized' (the body-length
    guard below is the real backstop)."""
    headers = getattr(r, "headers", None)
    if not headers:
        return False
    try:
        cl = headers.get("Content-Length") or headers.get("content-length")
    except Exception:  # noqa: BLE001 — odd header object; defer to the body-length guard
        return False
    if cl is None:
        return False
    try:
        return int(cl) > max_bytes
    except (TypeError, ValueError):
        return False


def get_bytes(
    client: Any,
    url: str,
    *,
    timeout: float = 8.0,
    headers: dict | None = None,
    params: dict | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> bytes | None:
    """GET ``url`` and return the raw response body, or ``None`` on ANY failure.

    Fail-soft: catches every exception (no client, network error, non-2xx via raise_for_status,
    weird response object) so callers never have to guard the call site. The body is also size-
    capped at ``max_bytes`` (untrusted public feeds): an advertised or actual body over the cap is
    ABORTED and returns ``None`` rather than buffering unbounded memory."""
    if client is None:
        return None
    try:
        r = client.get(
            url,
            headers={"User-Agent": DEFAULT_UA, **(headers or {})},
            params=params,
            timeout=timeout,
        )
        r.raise_for_status()
        if _oversized_content_length(r, max_bytes):
            return None
        content = r.content
        content = content if isinstance(content, (bytes, bytearray)) else bytes(content)
        if len(content) > max_bytes:
            return None
        return bytes(content)
    except Exception:
        return None


def get_json(
    client: Any,
    url: str,
    *,
    timeout: float = 8.0,
    headers: dict | None = None,
    params: dict | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> Any | None:
    """GET ``url`` and return the parsed JSON, or ``None`` on ANY failure (network or decode).

    Size-capped like :func:`get_bytes`: an oversized body is aborted (``None``) before parse so a
    hostile feed can't balloon memory."""
    if client is None:
        return None
    try:
        r = client.get(
            url,
            headers={"User-Agent": DEFAULT_UA, **(headers or {})},
            params=params,
            timeout=timeout,
        )
        r.raise_for_status()
        if _oversized_content_length(r, max_bytes):
            return None
        # Guard the actual body size before parsing: a response that advertises no Content-Length
        # but ships a huge body must still be capped.
        content = getattr(r, "content", None)
        if isinstance(content, (bytes, bytearray)) and len(content) > max_bytes:
            return None
        return r.json()
    except Exception:
        return None
