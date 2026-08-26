from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from statistics import median
from typing import Sequence

from .providers import VideoResult

KEYWORDS = {
    "official": 0.10, "original": 0.10, "extended": 0.12, "audio": 0.05,
    "radio edit": -0.10, "live": -0.35, "concert": -0.35, "karaoke": -0.55,
    "cover": -0.40, "tutorial": -0.65, "reaction": -0.65,
}
NOISE = re.compile(r"\b(?:official|music|audio|video|lyrics?|hd|hq|remaster(?:ed)?)\b", re.I)
NON_WORD = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ScoredVideo:
    result: VideoResult
    total: float
    components: dict[str, float]
    explanation: tuple[str, ...]


def _normalize(value: str, *, remove_noise: bool = False) -> str:
    value = value.casefold()
    if remove_noise:
        value = NOISE.sub("", value)
    return NON_WORD.sub(" ", value).strip()


def _similarity(expected: str, candidate: str) -> float:
    left, right = _normalize(expected, remove_noise=True), _normalize(candidate, remove_noise=True)
    if not left or not right:
        return 0.0
    direct = SequenceMatcher(None, left, right).ratio()
    containment = 1.0 if left in right else 0.0
    token = SequenceMatcher(None, " ".join(sorted(left.split())), " ".join(sorted(right.split()))).ratio()
    return max(direct, token, containment)


def score_youtube_results(
    results: Sequence[VideoResult], *, artist: str, title: str, preferred_keywords: Sequence[str] = ()
) -> list[ScoredVideo]:
    if not results:
        return []
    durations = [item.duration_seconds for item in results if item.duration_seconds and 90 <= item.duration_seconds <= 1200]
    center = median(durations) if durations else None
    max_views = max((item.view_count or 0 for item in results), default=0)
    scored: list[ScoredVideo] = []
    count = len(results)
    for index, item in enumerate(results):
        rank = 1.0 if count == 1 else 1.0 - index / (count - 1)
        title_match = _similarity(title, item.title)
        artist_match = max(_similarity(artist, item.channel), _similarity(artist, item.title))
        views = math.log1p(item.view_count or 0) / max(1.0, math.log1p(max_views))
        duration = 0.5 if center is None or item.duration_seconds is None else math.exp(-0.5 * ((item.duration_seconds - center) / 45) ** 2)
        haystack = _normalize(f"{item.title} {item.description}")
        keyword = sum(weight for word, weight in KEYWORDS.items() if word in haystack)
        keyword += sum(0.08 for word in preferred_keywords if _normalize(word) in haystack)
        total = 0.12 * rank + 0.12 * views + 0.31 * title_match + 0.27 * artist_match + 0.12 * duration + 0.06 * max(0, 1 + keyword)
        total = max(0.0, min(1.0, total + min(0.0, keyword)))
        reasons = [
            f"title match {title_match:.0%}", f"artist evidence {artist_match:.0%}",
            f"duration-cluster fit {duration:.0%}", f"result popularity {views:.0%}",
        ]
        if keyword:
            reasons.append(f"version signals {keyword:+.2f}")
        scored.append(ScoredVideo(item, round(total, 4), {
            "rank": round(rank, 4), "views": round(views, 4), "title": round(title_match, 4),
            "artist": round(artist_match, 4), "duration": round(duration, 4), "keywords": round(keyword, 4),
        }, tuple(reasons)))
    return sorted(scored, key=lambda item: (-item.total, item.result.id))

