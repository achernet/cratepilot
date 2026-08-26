from __future__ import annotations

import hashlib
import re
import unicodedata

_VERSION = re.compile(r"\s*[\[(](.*?)[])]\s*$")
_NOISE = re.compile(r"\b(?:official|audio|video|lyrics?|hq|hd|remaster(?:ed)?)\b", re.I)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return _NON_WORD.sub(" ", value).strip()


def split_version(title: str) -> tuple[str, str | None]:
    match = _VERSION.search(title)
    if not match:
        return title.strip(), None
    label = match.group(1).strip()
    if not re.search(r"mix|edit|remix|version|live|extended|dub|remaster", label, re.I):
        return title.strip(), None
    return title[: match.start()].strip(), label


def canonical_identity(artist: str, title: str, version: str | None = None) -> str:
    base_title, inferred_version = split_version(title)
    version = version or inferred_version or ""
    clean_title = _NOISE.sub("", base_title)
    return "|".join((normalize_text(artist.removeprefix("The ")), normalize_text(clean_title), normalize_text(version)))


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:20]
    return f"{prefix}_{digest}"

