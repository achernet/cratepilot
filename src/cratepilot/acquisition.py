from __future__ import annotations

import dataclasses
import hashlib
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Sequence

from platformdirs import user_data_path

from .identity import canonical_identity, stable_id
from .models import AcquisitionJobV1, LocalAssetV1
from .storage import Store

ACKNOWLEDGEMENT_VERSION = "permissive-acquisition-v1"


class AcquisitionError(RuntimeError):
    pass


class AudioVerifier(Protocol):
    def verify(self, path: Path, *, artist: str, title: str, samples: int, seconds: int, majority: int) -> dict[str, Any]: ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return re.sub(r"\s+", " ", value)[:160] or "Unknown"


class AcquisitionService:
    def __init__(self, store: Store, *, verifier: AudioVerifier, root: Path | None = None) -> None:
        self.store = store
        self.verifier = verifier
        self.root = (root or user_data_path("CratePilot", "Chernetz") / "acquisitions").resolve()
        self.source_root = self.root / "sources"
        self.derivative_root = self.root / "dj-library"
        self.quarantine_root = self.root / "quarantine"

    def acknowledge(self, accepted: bool) -> None:
        self.store.set_preference("permissive_acquisition", {
            "accepted": bool(accepted), "version": ACKNOWLEDGEMENT_VERSION,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def is_acknowledged(self) -> bool:
        value = self.store.preference("permissive_acquisition", {})
        return bool(value.get("accepted")) and value.get("version") == ACKNOWLEDGEMENT_VERSION

    def approve(self, candidate_ids: Sequence[str]) -> AcquisitionJobV1:
        if not candidate_ids:
            raise AcquisitionError("At least one candidate must be approved.")
        if len(candidate_ids) > 30:
            raise AcquisitionError("An acquisition review batch cannot exceed 30 candidates.")
        for candidate_id in candidate_ids:
            if self.store.candidate(candidate_id) is None:
                raise AcquisitionError(f"Unknown candidate: {candidate_id}")
        now = datetime.now(UTC).isoformat()
        job = AcquisitionJobV1(
            id=stable_id("acquire", now, *candidate_ids), candidate_ids=tuple(candidate_ids), approved_at=now,
            acknowledgement_version=ACKNOWLEDGEMENT_VERSION if self.is_acknowledged() else None,
        )
        self.store.save_acquisition_job(job)
        return job

    def run(self, job: AcquisitionJobV1) -> AcquisitionJobV1:
        if not self.is_acknowledged() or job.acknowledgement_version != ACKNOWLEDGEMENT_VERSION:
            raise AcquisitionError("Permissive acquisition is disabled. Use the legal source links or acknowledge it locally.")
        attempts: list[dict[str, Any]] = []
        assets: list[LocalAssetV1] = []
        quarantine: list[str] = []
        candidates = [self.store.candidate(candidate_id) for candidate_id in job.candidate_ids]
        by_track: dict[str, list] = {}
        for candidate in candidates:
            if candidate:
                by_track.setdefault(candidate.catalog_track_id, []).append(candidate)
        for track_id, choices in by_track.items():
            track = self.store.catalog_track(track_id)
            if not track:
                continue
            success = False
            for candidate in sorted(choices, key=lambda item: (item.rank, -item.total_score))[:3]:
                try:
                    generated = self._acquire_candidate(candidate.source_url, track.artist, track.title, track.year)
                    verification = self.verifier.verify(
                        generated[0], artist=track.artist, title=track.title, samples=11, seconds=12, majority=6,
                    )
                    actual_identity = canonical_identity(
                        str(verification.get("artist", "")), str(verification.get("title", ""))
                    )
                    if actual_identity.split("|")[:2] != track.normalized_identity.split("|")[:2]:
                        quarantined = self._quarantine(generated[0], candidate.id)
                        quarantine.append(str(quarantined))
                        attempts.append({"candidate_id": candidate.id, "status": "identity_mismatch", "verification": verification})
                        continue
                    source_asset, dj_asset = self._promote(generated[0], track.artist, track.title, verification.get("year") or track.year)
                    assets.extend((source_asset, dj_asset))
                    updated = dataclasses.replace(
                        track, verification_state="verified", assets=tuple(dict.fromkeys(track.assets + (source_asset, dj_asset))),
                        year=verification.get("year") or track.year,
                        year_source=verification.get("year_source") or track.year_source,
                        year_confidence=verification.get("year_confidence") or track.year_confidence,
                    )
                    self.store.save_catalog_track(updated)
                    attempts.append({"candidate_id": candidate.id, "status": "verified", "verification": verification})
                    success = True
                    break
                except Exception as exc:
                    attempts.append({"candidate_id": candidate.id, "status": "failed", "error": str(exc)})
            if not success:
                attempts.append({"catalog_track_id": track_id, "status": "exhausted"})
        status = "complete" if all(item.get("status") != "exhausted" for item in attempts) else "partial"
        updated_job = dataclasses.replace(
            job, status=status, attempts=tuple(attempts), generated_assets=tuple(assets),
            quarantine_paths=tuple(quarantine), message=f"Created {len(assets) // 2} verified DJ derivatives.",
        )
        self.store.save_acquisition_job(updated_job)
        return updated_job

    def _acquire_candidate(self, url: str, artist: str, title: str, year: int | None) -> tuple[Path, str]:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="download-", dir=self.root))
        template = temporary / "source.%(ext)s"
        command = [
            "yt-dlp", "--no-playlist", "--retries", "3", "--fragment-retries", "3",
            "-f", "bestaudio/best", "-o", str(template), url,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            raise AcquisitionError(f"Download failed: {exc}") from exc
        files = [item for item in temporary.iterdir() if item.is_file()]
        if len(files) != 1 or files[0].stat().st_size == 0:
            shutil.rmtree(temporary, ignore_errors=True)
            raise AcquisitionError("Downloader did not produce one valid source file.")
        return files[0], str(temporary)

    def _promote(self, source: Path, artist: str, title: str, year: int | None) -> tuple[LocalAssetV1, LocalAssetV1]:
        source_hash = _sha256(source)
        source_target = self.source_root / source_hash[:2] / f"{source_hash}{source.suffix.lower()}"
        source_target.parent.mkdir(parents=True, exist_ok=True)
        if not source_target.exists():
            shutil.move(str(source), source_target)
        label = f"{sanitize_component(artist)} - {sanitize_component(title)}"
        if year:
            label += f" ({int(year)})"
        self.derivative_root.mkdir(parents=True, exist_ok=True)
        target = self.derivative_root / f"{label}.mp3"
        if target.exists():
            target = self.derivative_root / f"{label} [{source_hash[:8]}].mp3"
        staged = target.with_suffix(".staged.mp3")
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(source_target), "-vn", "-codec:a", "libmp3lame", "-b:a", "320k", str(staged)],
                check=True, capture_output=True, text=True, timeout=900,
            )
            subprocess.run(["mp3gain", "-r", "-k", "-p", str(staged)], check=True, capture_output=True, text=True, timeout=180)
            staged.replace(target)
        except (OSError, subprocess.SubprocessError) as exc:
            staged.unlink(missing_ok=True)
            raise AcquisitionError(f"DJ derivative creation failed: {exc}") from exc
        return (
            LocalAssetV1("source", str(source_target), source_hash, immutable=True),
            LocalAssetV1("dj-mp3", str(target), _sha256(target), immutable=False, mime_type="audio/mpeg"),
        )

    def _quarantine(self, source: Path, candidate_id: str) -> Path:
        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        target = self.quarantine_root / f"{candidate_id}-{_sha256(source)[:12]}{source.suffix.lower()}"
        shutil.move(str(source), target)
        return target

