from __future__ import annotations

import json
import random
import subprocess
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


class RecognitionError(RuntimeError):
    pass


def _duration(path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RecognitionError(f"Could not inspect audio duration: {exc}") from exc


def _recognize(path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["songrec", "recognize", "-j", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        payload = json.loads(result.stdout)
    except FileNotFoundError as exc:
        raise RecognitionError("songrec is required on PATH for identity verification.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RecognitionError("songrec recognition timed out.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown error").strip()
        raise RecognitionError(f"songrec recognition failed: {detail}") from exc
    except json.JSONDecodeError as exc:
        raise RecognitionError("songrec returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise RecognitionError("songrec returned an unexpected JSON document.")
    return payload


def _musicbrainz_year(artist: str, title: str) -> tuple[int | None, float]:
    query = urllib.parse.urlencode({"query": f'artist:"{artist}" AND recording:"{title}"', "fmt": "json", "limit": 25})
    request = urllib.request.Request(
        f"https://musicbrainz.org/ws/2/recording?{query}",
        headers={"User-Agent": "CratePilot/0.2 (https://github.com/achernet/cratepilot)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            recordings = json.load(response).get("recordings", [])
    except Exception:
        return None, 0.0
    years: list[tuple[int, int]] = []
    for recording in recordings:
        score = int(recording.get("score", 0))
        for release in recording.get("releases", []):
            date = str(release.get("date", ""))
            if len(date) >= 4 and date[:4].isdigit():
                years.append((int(date[:4]), score))
    if not years:
        return None, 0.0
    year, score = min(years, key=lambda item: item[0])
    return year, min(1.0, score / 100)


class ShazamMusicBrainzVerifier:
    def verify(self, path: Path, *, artist: str, title: str, samples: int = 11, seconds: int = 12, majority: int = 6) -> dict[str, Any]:
        duration = _duration(path)
        if duration < seconds:
            raise RecognitionError("Track is shorter than the requested recognition sample.")
        generator = random.Random(f"{path.stat().st_size}:{path.name}:{samples}:{seconds}")
        starts = [generator.uniform(0, max(0.0, duration - seconds)) for _ in range(samples)]
        votes: Counter[tuple[str, str]] = Counter()
        evidence: list[dict[str, Any]] = []
        winning_raw: dict[str, Any] | None = None
        with tempfile.TemporaryDirectory(prefix="cratepilot-recognition-") as temporary:
            for index, start in enumerate(starts):
                sample = Path(temporary) / f"sample-{index:02d}.wav"
                try:
                    subprocess.run(
                        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", str(seconds), "-i", str(path), "-ac", "1", "-ar", "44100", str(sample)],
                        check=True, capture_output=True, text=True, timeout=60,
                    )
                    raw = _recognize(sample)
                    track = raw.get("track", {})
                    detected = (str(track.get("subtitle", "")).strip(), str(track.get("title", "")).strip())
                    if not all(detected):
                        continue
                    votes[detected] += 1
                    evidence.append({"start_seconds": round(start, 3), "artist": detected[0], "title": detected[1]})
                    if votes[detected] >= majority:
                        winning_raw = raw
                        break
                except (OSError, subprocess.SubprocessError, RecognitionError):
                    continue
        if not votes:
            raise RecognitionError("No sampled segment was recognized.")
        (detected_artist, detected_title), count = votes.most_common(1)[0]
        if count < majority:
            raise RecognitionError(f"Recognition did not reach consensus ({count}/{samples}; {majority} required).")
        year, confidence = _musicbrainz_year(detected_artist, detected_title)
        return {
            "artist": detected_artist, "title": detected_title, "consensus": count,
            "samples_requested": samples, "sample_seconds": seconds, "evidence": evidence,
            "year": year, "year_source": "musicbrainz" if year else None, "year_confidence": confidence,
            "shazam_url": (winning_raw or {}).get("track", {}).get("url"),
        }
