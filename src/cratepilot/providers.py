from __future__ import annotations

import base64
import html
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderTrack:
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
    id: str
    title: str
    channel: str
    url: str
    duration_seconds: float | None = None
    view_count: int | None = None
    description: str = ""


class SimilarityProvider(Protocol):
    def related(self, track: ProviderTrack, *, same_artist_limit: int, similar_limit: int) -> Sequence[ProviderTrack]: ...


class VideoSearchProvider(Protocol):
    def search(self, artist: str, title: str, *, limit: int = 30) -> Sequence[VideoResult]: ...


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
    def search(self, artist: str, title: str, *, limit: int = 30) -> Sequence[VideoResult]:
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
        evidence = track.evidence or {}
        shazam_url = evidence.get("shazam_url")
        if not shazam_url and evidence.get("path"):
            result = self.verifier.verify(
                Path(str(evidence["path"])), artist=track.artist, title=track.title,
                samples=11, seconds=12, majority=6,
            )
            shazam_url = result.get("shazam_url")
        if not shazam_url:
            return []
        request = urllib.request.Request(
            str(shazam_url), headers={"User-Agent": "Mozilla/5.0 CratePilot/0.2"},
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
            relationship = "same_artist" if artist.casefold() == track.artist.casefold() or "inAlbum" in item else "similar"
            target = same_artist if relationship == "same_artist" else similar
            target.append(ProviderTrack(
                artist=artist, title=title, provider="shazam", url=item.get("url"), relationship=relationship,
                weight=0.85 if relationship == "same_artist" else 0.70, evidence={"shazam_seed": shazam_url},
            ))
        return (*same_artist[:same_artist_limit], *similar[:similar_limit])
