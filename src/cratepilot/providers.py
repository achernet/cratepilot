"""External metadata, similarity, and video-search provider adapters.

Providers return small, source-labelled records; catalog deduplication and
policy decisions remain in the service layer.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol, Sequence

from .identity import normalize_text

LOGGER = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Raised when an external provider cannot fulfill a request."""

    pass

# Shazam does not provide a public API for related tracks, but the web page for a recognized track includes a
# JSON array of citations that can be parsed to find similar tracks. The following headers are required, or
# Shazam will return invalid information. The User-Agent is a recent Chrome on Android, and the Sec-CH-UA
# header is required to avoid a 403 response.
SHAZAM_SIMILARITY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; K) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Mobile Safari/537.36"
    ),
    "Sec-CH-UA": (
        '"Not=A?Brand";v="99", '
        '"Google Chrome";v="151", '
        '"Chromium";v="151"'
    ),
}

@dataclass(frozen=True)
class ProviderTrack:
    """Provider-neutral metadata for one seed or related track."""

    artist: str
    title: str
    provider: str
    external_id: str | None = None
    url: str | None = None
    isrc: str | None = None
    relationship: str = "seed"
    weight: float = 1.0
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class VideoResult:
    """Minimal video-search result used by the acquisition scorer."""

    id: str
    title: str
    channel: str
    url: str
    duration_seconds: float | None = None
    view_count: int | None = None
    description: str = ""


class SimilarityProvider(Protocol):
    """Interface for expanding a track into same-artist and similar tracks."""

    def related(self, track: ProviderTrack, *, same_artist_limit: int, similar_limit: int) -> Sequence[ProviderTrack]: ...


class VideoSearchProvider(Protocol):
    """Interface for finding source candidates without downloading them."""

    def search(self, artist: str, title: str, *, limit: int = 30) -> Sequence[VideoResult]: ...


def _identity_score(track: ProviderTrack, *, artist: str, title: str) -> float:
    """Score provider metadata against a seed without requiring exact spelling."""

    candidate_artist = normalize_text(artist)
    candidate_title = normalize_text(title)
    expected_artist = normalize_text(track.artist)
    expected_title = normalize_text(track.title)
    if not candidate_artist or not candidate_title or not expected_artist or not expected_title:
        return 0.0
    return 0.45 * SequenceMatcher(None, expected_artist, candidate_artist).ratio() + 0.55 * SequenceMatcher(
        None, expected_title, candidate_title
    ).ratio()


class SpotifyMetadataProvider:
    """Resolve public Spotify track/playlist URLs. Audio is never requested."""

    _URL = re.compile(r"^https://open\.spotify\.com/(track|playlist)/([A-Za-z0-9]+)(?:[/?].*)?$")

    def __init__(self, *, client_id: str | None = None, client_secret: str | None = None, timeout: int = 12) -> None:
        self.client_id = client_id or os.environ.get("CRATEPILOT_SPOTIFY_CLIENT_ID") or self._keyring("client-id")
        self.client_secret = (
            client_secret or os.environ.get("CRATEPILOT_SPOTIFY_CLIENT_SECRET") or self._keyring("client-secret")
        )
        self.timeout = timeout

    @staticmethod
    def _keyring(name: str) -> str | None:
        try:
            import keyring

            return keyring.get_password("CratePilot/Spotify", name)
        except Exception:
            return None

    def _json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except Exception as exc:
            raise ProviderError(f"Spotify metadata request failed: {exc}") from exc

    def _token(self) -> str:
        if not self.client_id or not self.client_secret:
            raise ProviderError(
                "Spotify metadata requires CRATEPILOT_SPOTIFY_CLIENT_ID and CRATEPILOT_SPOTIFY_CLIENT_SECRET."
            )
        authorization = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        request = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            headers={"Authorization": f"Basic {authorization}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        return str(self._json(request)["access_token"])

    def resolve(self, url: str) -> list[ProviderTrack]:
        """Resolve a public Spotify track or first-page playlist URL to metadata."""

        match = self._URL.fullmatch(url.strip())
        if not match:
            raise ValueError("Only public open.spotify.com track and playlist URLs are supported.")
        kind, external_id = match.groups()
        token = self._token()
        request = urllib.request.Request(
            f"https://api.spotify.com/v1/{kind}s/{external_id}", headers={"Authorization": f"Bearer {token}"}
        )
        payload = self._json(request)
        values = [payload] if kind == "track" else [item.get("track") for item in payload.get("tracks", {}).get("items", [])]
        tracks: list[ProviderTrack] = []
        for item in values:
            if not item or item.get("is_local"):
                continue
            tracks.append(
                ProviderTrack(
                    artist=", ".join(artist["name"] for artist in item.get("artists", [])),
                    title=item["name"], provider="spotify", external_id=item["id"],
                    url=item.get("external_urls", {}).get("spotify"), isrc=item.get("external_ids", {}).get("isrc"),
                    evidence={"album": item.get("album", {}).get("name"), "duration_ms": item.get("duration_ms")},
                )
            )
        return tracks


class YtDlpSearchProvider:
    """Search YouTube metadata through yt-dlp's flat-playlist mode."""

    def search(self, artist: str, title: str, *, limit: int = 30) -> Sequence[VideoResult]:
        """Return up to ``limit`` metadata-only YouTube search results."""

        import subprocess

        limit = max(1, min(100, int(limit)))
        query = f"ytsearch{limit}:{artist} {title}"
        command = ["yt-dlp", "--dump-single-json", "--flat-playlist", "--no-warnings", query]
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=90)
            entries = json.loads(result.stdout).get("entries", [])
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise ProviderError(f"YouTube search failed: {exc}") from exc
        return [
            VideoResult(
                id=str(item.get("id", "")), title=str(item.get("title", "")),
                channel=str(item.get("channel") or item.get("uploader") or ""),
                url=str(item.get("webpage_url") or f"https://www.youtube.com/watch?v={item.get('id', '')}"),
                duration_seconds=item.get("duration"), view_count=item.get("view_count"),
                description=str(item.get("description") or ""),
            )
            for item in entries if item and item.get("id")
        ]


class DeezerSimilarityProvider:
    """Expand metadata-only seeds through Deezer's public catalog graph."""

    def __init__(self, *, timeout: int = 12, similar_artist_cap: int = 5) -> None:
        self.timeout = timeout
        # Fetching a few top tracks from several related artists provides
        # variety without turning each graph node into dozens of HTTP calls.
        self.similar_artist_cap = max(1, min(10, similar_artist_cap))

    def _json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": "CratePilot/0.3"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except Exception as exc:
            raise ProviderError(f"Deezer metadata request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Deezer returned an unexpected document.")
        if payload.get("error"):
            raise ProviderError(f"Deezer metadata request failed: {payload['error']}")
        return payload

    def _resolve(self, track: ProviderTrack) -> tuple[str, str] | None:
        evidence = track.evidence or {}
        if track.provider == "deezer" and track.external_id and evidence.get("deezer_artist_id"):
            return str(track.external_id), str(evidence["deezer_artist_id"])
        query = urllib.parse.urlencode(
            {"q": f'artist:"{track.artist}" track:"{track.title}"', "limit": 5}
        )
        payload = self._json(f"https://api.deezer.com/search/track?{query}")
        candidates = [item for item in payload.get("data", []) if isinstance(item, dict)]
        if not candidates:
            LOGGER.warning("Deezer found no seed match for %s — %s", track.artist, track.title)
            return None
        best = max(
            candidates,
            key=lambda item: _identity_score(
                track,
                artist=str(item.get("artist", {}).get("name", "")),
                title=str(item.get("title", "")),
            ),
        )
        score = _identity_score(
            track,
            artist=str(best.get("artist", {}).get("name", "")),
            title=str(best.get("title", "")),
        )
        artist_id = best.get("artist", {}).get("id")
        if score < 0.72 or not best.get("id") or not artist_id:
            LOGGER.warning(
                "Rejected weak Deezer seed match for %s — %s (identity score %.0f%%)",
                track.artist,
                track.title,
                score * 100,
            )
            return None
        LOGGER.info(
            "Resolved %s — %s to Deezer track %s (identity score %.0f%%)",
            track.artist,
            track.title,
            best["id"],
            score * 100,
        )
        return str(best["id"]), str(artist_id)

    @staticmethod
    def _provider_track(item: dict[str, Any], relationship: str, seed_artist_id: str) -> ProviderTrack | None:
        artist = item.get("artist", {})
        if not isinstance(artist, dict) or not item.get("id") or not artist.get("name") or not item.get("title"):
            return None
        return ProviderTrack(
            artist=str(artist["name"]),
            title=str(item["title"]),
            provider="deezer",
            external_id=str(item["id"]),
            url=str(item.get("link") or "") or None,
            isrc=str(item.get("isrc") or "") or None,
            relationship=relationship,
            weight=0.85 if relationship == "same_artist" else 0.70,
            evidence={"deezer_artist_id": str(artist.get("id", "")), "deezer_seed_artist_id": seed_artist_id},
        )

    def related(self, track: ProviderTrack, *, same_artist_limit: int, similar_limit: int) -> Sequence[ProviderTrack]:
        """Return same-artist top tracks plus top tracks from related artists."""

        resolved = self._resolve(track)
        if not resolved:
            return ()
        track_id, artist_id = resolved
        same_payload = self._json(
            f"https://api.deezer.com/artist/{artist_id}/top?"
            + urllib.parse.urlencode({"limit": same_artist_limit + 1})
        )
        same_artist: list[ProviderTrack] = []
        for item in same_payload.get("data", []):
            value = self._provider_track(item, "same_artist", artist_id)
            if value and value.external_id != track_id:
                same_artist.append(value)
        related_payload = self._json(
            f"https://api.deezer.com/artist/{artist_id}/related?"
            + urllib.parse.urlencode({"limit": min(self.similar_artist_cap, similar_limit)})
        )
        related_artists = [
            item for item in related_payload.get("data", []) if isinstance(item, dict) and item.get("id")
        ][: min(self.similar_artist_cap, similar_limit)]
        per_artist = max(1, (similar_limit + max(1, len(related_artists)) - 1) // max(1, len(related_artists)))
        similar: list[ProviderTrack] = []
        for artist in related_artists:
            try:
                top = self._json(
                    f"https://api.deezer.com/artist/{artist['id']}/top?"
                    + urllib.parse.urlencode({"limit": per_artist})
                )
            except ProviderError as exc:
                LOGGER.warning("Could not load top tracks for related Deezer artist %s: %s", artist["id"], exc)
                continue
            for item in top.get("data", []):
                value = self._provider_track(item, "similar", artist_id)
                if value:
                    similar.append(value)
        LOGGER.info(
            "Deezer returned %d same-artist and %d similar-artist tracks for %s — %s",
            min(len(same_artist), same_artist_limit),
            min(len(similar), similar_limit),
            track.artist,
            track.title,
        )
        return (*same_artist[:same_artist_limit], *similar[:similar_limit])


class FallbackSimilarityProvider:
    """Try similarity providers in order until one returns usable neighbors."""

    def __init__(self, *providers: SimilarityProvider) -> None:
        if not providers:
            raise ValueError("At least one similarity provider is required.")
        self.providers = providers

    def related(self, track: ProviderTrack, *, same_artist_limit: int, similar_limit: int) -> Sequence[ProviderTrack]:
        """Return the first non-empty provider result, logging recoverable failures."""

        last_error: Exception | None = None
        for provider in self.providers:
            try:
                related = provider.related(
                    track,
                    same_artist_limit=same_artist_limit,
                    similar_limit=similar_limit,
                )
            except Exception as exc:
                last_error = exc
                LOGGER.warning("%s could not expand %s — %s: %s", type(provider).__name__, track.artist, track.title, exc)
                continue
            if related:
                return related
        if last_error:
            raise ProviderError(f"All similarity providers failed; last error: {last_error}") from last_error
        return ()


class ShazamRelatedProvider:
    """Expand a recognized local seed using Shazam's public related-track page metadata."""

    def __init__(self, verifier=None, *, timeout: int = 12) -> None:
        if verifier is None:
            from .recognition import ShazamMusicBrainzVerifier
            verifier = ShazamMusicBrainzVerifier()
        self.verifier = verifier
        self.timeout = timeout

    @staticmethod
    def _citations(document: str) -> list[dict[str, Any]]:
        text = html.unescape(document)
        variants = (text, text.replace(r'\"', '"').replace(r"\\/", "/"))
        decoder = json.JSONDecoder()
        for variant in variants:
            for match in re.finditer(r'"citation"\s*:\s*', variant):
                try:
                    value, _ = decoder.raw_decode(variant[match.end():])
                except json.JSONDecodeError:
                    continue
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def related(self, track: ProviderTrack, *, same_artist_limit: int, similar_limit: int) -> Sequence[ProviderTrack]:
        """Return related tracks for seeds with Shazam page evidence."""

        evidence = track.evidence or {}
        shazam_url = evidence.get("shazam_url")
        if not shazam_url and track.provider == "shazam" and track.url:
            shazam_url = track.url
        if not shazam_url and evidence.get("path"):
            result = self.verifier.verify(
                Path(str(evidence["path"])), artist=track.artist, title=track.title,
                samples=11, seconds=12, majority=6,
            )
            shazam_url = result.get("shazam_url")
        if not shazam_url:
            return ()
        LOGGER.info("Expanding Shazam relationships for %s — %s", track.artist, track.title)
        request = urllib.request.Request(
            str(shazam_url),
            headers=SHAZAM_SIMILARITY_HEADERS,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                document = response.read().decode("utf-8", "replace")
        except Exception as exc:
            raise ProviderError(f"Shazam related-track request failed: {exc}") from exc
        same_artist: list[ProviderTrack] = []
        similar: list[ProviderTrack] = []
        seen: set[tuple[str, str]] = set()
        for item in self._citations(document):
            artist_value = item.get("byArtist", "")
            if isinstance(artist_value, dict):
                artist_value = artist_value.get("name", "")
            artist = str(artist_value)
            title = str(item.get("name", ""))
            key = (artist.casefold(), title.casefold())
            if not all(key) or key in seen or key == (track.artist.casefold(), track.title.casefold()):
                continue
            seen.add(key)
            relationship = "same_artist" if normalize_text(artist) == normalize_text(track.artist) else "similar"
            target = same_artist if relationship == "same_artist" else similar
            target.append(ProviderTrack(
                artist=artist, title=title, provider="shazam", url=item.get("url"), relationship=relationship,
                weight=0.85 if relationship == "same_artist" else 0.70, evidence={"shazam_seed": shazam_url},
            ))
        selected = (*same_artist[:same_artist_limit], *similar[:similar_limit])
        LOGGER.info(
            "Shazam returned %d same-artist and %d similar-artist tracks for %s — %s",
            min(len(same_artist), same_artist_limit),
            min(len(similar), similar_limit),
            track.artist,
            track.title,
        )
        return selected
