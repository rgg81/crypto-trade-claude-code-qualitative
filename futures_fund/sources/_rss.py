"""Shared namespace-aware RSS/Atom parsing for the source adapters.

Forked from base ``vendors.parse_rss`` / ``_rss_text`` / ``_clean_html`` but returns plain dicts
(``title``, ``url``, ``body``, ``published``) so each adapter can mint its own ContentItem with its
own source name. Malformed XML -> ``[]`` (never raises)."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET

_ATOM = "{http://www.w3.org/2005/Atom}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"  # <content:encoded> full-body namespace
_MEDIA = "{http://search.yahoo.com/mrss/}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_html(s: str | None, limit: int = 500) -> str:
    """Strip HTML tags, decode entities, collapse whitespace, truncate. '' on None.

    (The base ``vendors._clean_html`` cleaner, lifted so adapters that scrape raw HTML — telegram,
    forums — share one implementation.)"""
    if not s:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(s))).strip()
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _rss_text(el, tag: str) -> str | None:
    for cand in (tag, _ATOM + tag):
        e = el.find(cand)
        if e is not None:
            if e.text and e.text.strip():
                return e.text.strip()
            if e.get("href"):
                return e.get("href")
    return None


def parse_feed(content: bytes, body_limit: int = 500) -> list[dict]:
    """Parse an RSS/Atom feed into a list of ``{title, url, body, published, author}`` dicts.

    Namespace-aware (RSS items + Atom entries; ``content:encoded``/``description``/``summary`` for
    the body). Returns ``[]`` on malformed/empty XML — never raises."""
    if not content:
        return []
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, TypeError, ValueError):
        return []
    nodes = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    out: list[dict] = []
    for n in nodes:
        try:
            title = _rss_text(n, "title")
            if not title:
                continue
            raw_body = (
                _rss_text(n, _CONTENT + "encoded")
                or _rss_text(n, "encoded")
                or _rss_text(n, "description")
                or _rss_text(n, "content")
                or _rss_text(n, "summary")
                or _media_description(n)
            )
            out.append(
                {
                    "title": title,
                    "url": _rss_text(n, "link") or _rss_text(n, "guid") or "",
                    "body": clean_html(raw_body, body_limit),
                    "published": (
                        _rss_text(n, "pubDate")
                        or _rss_text(n, "published")
                        or _rss_text(n, "updated")
                        or ""
                    ),
                    "author": _rss_text(n, "creator")
                    or _rss_text(n, "author")
                    or _atom_author(n)
                    or "",
                }
            )
        except Exception:
            continue  # skip the one bad node, keep the rest
    return out


def _media_description(n) -> str | None:
    e = n.find(f"{_MEDIA}group/{_MEDIA}description") or n.find(f"{_MEDIA}description")
    return e.text if e is not None else None


def _atom_author(n) -> str | None:
    e = n.find(f"{_ATOM}author/{_ATOM}name") or n.find("author/name")
    return e.text.strip() if e is not None and e.text else None
