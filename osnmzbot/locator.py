"""Deciding whether a chat message is a collection reference.

Upstream parse_collection_locator remains the real authority; this only
decides whether it is worth calling, so ordinary chat never opens a session.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import re
from urllib.parse import urlsplit

SLUG_SHAPE = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")


BARE_SLUG_MIN_LENGTH = 5


ADDRESS_SHAPE = re.compile(r"0x[0-9a-fA-F]{40}\Z")


def looks_like_locator(text: str) -> bool:
    """True when a free-text message is worth trying to parse as a collection.

    Cheap pre-filter so ordinary chat never triggers an OpenSea round-trip. The
    library's parse_collection_locator stays the real authority.
    """
    value = text.strip()
    if not value:
        return False
    if value.startswith(("http://", "https://")):
        # Only a real /collection/<slug> path, matching the library, which
        # rejects /, /item/<id>, and any other host.
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        if parsed.scheme != "https" or parsed.hostname != "opensea.io":
            return False
        segments = [s for s in parsed.path.split("/") if s]
        return len(segments) >= 2 and segments[0] == "collection" and bool(
            SLUG_SHAPE.fullmatch(segments[1])
        )
    if value.startswith("0x"):
        return len(value) == 42 and bool(ADDRESS_SHAPE.fullmatch(value))
    if not SLUG_SHAPE.fullmatch(value):
        return False
    if len(value) < BARE_SLUG_MIN_LENGTH:
        return False
    return any(ch.isdigit() or ch in "-_" for ch in value) or len(value) >= 8

