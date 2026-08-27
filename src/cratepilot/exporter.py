from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

from .legacy import djmix
from .models import ExportManifestV1, SetPlanV1, TrackAnalysisV1, write_json

LOGGER = logging.getLogger(__name__)


class ExportError(RuntimeError):
    pass


REKORDBOX_CHECKLIST = (
    "Import playlist.m3u8 into the latest Rekordbox in Export mode.",
    "Analyze the imported tracks and verify every beat grid at the first downbeat.",
    "Enter Hot Cues A/B/C from cue-sheet.html and rehearse each transition.",
    "Export the playlist from Rekordbox to two separately formatted FAT32 USB drives.",
    "Eject, reconnect, and verify both playlists and waveforms before leaving for the venue.",
    "Keep reference-mix.flac on a separate device as a safety copy.",
)


def sanitize_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " - ", value)
    value = re.sub(r"(?:\s*-\s*)+", " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value[:160] or "Untitled track"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(seconds: float) -> str:
    minutes, second = divmod(max(0, round(seconds)), 60)
    return f"{minutes:02d}:{second:02d}"


def _cue_sheet(plan: SetPlanV1, tracks: list[TrackAnalysisV1]) -> str:
    rows = []
    for index, track in enumerate(tracks):
        transition = plan.transitions[index - 1] if index else None
        transition_note = "Opening track"
        if transition:
            transition_note = (
                f"{transition.crossfade_bars} bars · bass at {transition.bass_handoff_fraction:.0%} · "
                f"{transition.fade_shape} fade" + (" · filter sweep" if transition.filter_sweep else "")
            )
        rows.append(
            "<tr>"
            f"<td>{index + 1:02d}</td><td><strong>{html.escape(track.title)}</strong><br><small>{html.escape(track.artist)}</small></td>"
            f"<td>{track.bpm:.1f}</td><td>{html.escape(track.camelot)}</td><td>{_timestamp(track.cues.hot_cue_a_seconds)}</td>"
            f"<td>{_timestamp(track.cues.hot_cue_b_seconds)}</td><td>{_timestamp(track.cues.hot_cue_c_seconds)}</td>"
            f"<td>{html.escape(transition_note)}</td></tr>"
        )
    checklist = "".join(f"<li>{html.escape(item)}</li>" for item in REKORDBOX_CHECKLIST)
    return f"""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{html.escape(plan.title)} cue sheet</title>
<style>body{{font:14px system-ui;margin:32px;color:#172018}}h1{{margin-bottom:4px}}p,small{{color:#5b665e}}table{{width:100%;border-collapse:collapse;margin:28px 0}}th,td{{padding:10px;border-bottom:1px solid #ccd3ce;text-align:left}}th{{font-size:11px;text-transform:uppercase}}@media print{{body{{margin:12mm}}}}</style></head>
<body><h1>{html.escape(plan.title)}</h1><p>{plan.duration_seconds / 60:.1f} minutes · CratePilot plan {html.escape(plan.id)}</p>
<table><thead><tr><th>#</th><th>Track</th><th>BPM</th><th>Key</th><th>Hot A</th><th>Hot B</th><th>Hot C</th><th>Transition</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table><h2>First-booth checklist</h2><ol>{checklist}</ol></body></html>"""


def write_rekordbox_package(
    plan: SetPlanV1,
    library: Mapping[str, TrackAnalysisV1],
    output_directory: Path,
    *,
    render_reference: bool = False,
    analysis_cache: Path | None = None,
) -> ExportManifestV1:
    output_directory = output_directory.expanduser().resolve()
    LOGGER.info("Exporting plan %s with %d tracks to %s", plan.id, len(plan.track_ids), output_directory)
    if output_directory.exists() and any(output_directory.iterdir()):
        raise ExportError(f"Export directory is not empty: {output_directory}")
    output_directory.mkdir(parents=True, exist_ok=True)
    tracks_directory = output_directory / "tracks"
    tracks_directory.mkdir()
    tracks: list[TrackAnalysisV1] = []
    copied: list[Path] = []
    for index, track_id in enumerate(plan.track_ids, 1):
        track = library.get(track_id)
        if track is None:
            raise ExportError(f"Plan references an unknown track: {track_id}")
        if not track.path:
            raise ExportError(f"Track has no local source path: {track.label}")
        source = Path(track.path).expanduser().resolve()
        if not source.is_file():
            raise ExportError(f"Track source is missing: {source}")
        filename = f"{index:02d} - {sanitize_filename(track.label)}{source.suffix.casefold()}"
        target = tracks_directory / filename
        shutil.copy2(source, target)
        tracks.append(track)
        copied.append(target)

    playlist = output_directory / "playlist.m3u8"
    playlist.write_text("#EXTM3U\n" + "\n".join(f"tracks/{path.name}" for path in copied) + "\n", encoding="utf-8")
    write_json(output_directory / "set-plan.json", plan)
    (output_directory / "cue-sheet.html").write_text(_cue_sheet(plan, tracks), encoding="utf-8")

    with (output_directory / "transition-notes.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("transition", "outgoing", "incoming", "score", "phrase_bars", "crossfade_bars", "bass_handoff", "filter", "warning"))
        for index, transition in enumerate(plan.transitions, 1):
            writer.writerow((
                index,
                tracks[index - 1].label,
                tracks[index].label,
                transition.total_score,
                transition.phrase_bars,
                transition.crossfade_bars,
                transition.bass_handoff_fraction,
                transition.filter_sweep,
                transition.warning or "",
            ))

    reference = output_directory / "reference-mix.flac"
    if render_reference:
        _render_reference(plan, tracks, reference, analysis_cache=analysis_cache)

    attribution = {
        "schema_version": 1,
        "notice": "Source audio remains owned and licensed by its respective rightsholders.",
        "tracks": [{"artist": track.artist, "title": track.title, "source_sha256": sha256(Path(track.path))} for track in tracks],
    }
    write_json(output_directory / "ATTRIBUTIONS.json", attribution)

    candidate_files = sorted(path for path in output_directory.rglob("*") if path.is_file())
    files = tuple(
        {
            "path": str(path.relative_to(output_directory)).replace("\\", "/"),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in candidate_files
    )
    source_timestamp = max(Path(track.path or "").stat().st_mtime for track in tracks)
    manifest = ExportManifestV1(
        plan_id=plan.id,
        created_at=dt.datetime.fromtimestamp(source_timestamp, tz=dt.timezone.utc).isoformat(),
        output_directory=".",
        files=files,
        duration_seconds=plan.duration_seconds,
        mastering={"reference_mix_rendered": render_reference, "integrated_lufs": -14.0, "true_peak_db": -1.0, "format": "FLAC"},
        rekordbox_checklist=REKORDBOX_CHECKLIST,
    )
    write_json(output_directory / "export-manifest.json", manifest)
    LOGGER.info("Completed Rekordbox export %s with %d generated files", plan.id, len(files))
    return manifest


def _render_reference(
    plan: SetPlanV1,
    tracks: list[TrackAnalysisV1],
    output: Path,
    *,
    analysis_cache: Path | None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="cratepilot-render-") as temporary:
        playlist = Path(temporary) / "plan.m3u8"
        lines = ["#EXTM3U"]
        for index, track in enumerate(tracks):
            if index:
                transition = plan.transitions[index - 1]
                lines.append(
                    "#DJMIX "
                    f"-P{transition.phrase_bars}C{transition.crossfade_bars}B{round(transition.bass_cut_db)}"
                    f"F{int(transition.filter_sweep)}H{transition.bass_handoff_fraction:.2f}S{transition.fade_shape[0].upper()}"
                )
            lines.append(str(Path(track.path or "").resolve()))
        playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
        arguments = [
            "render", str(playlist), "-o", str(output), "--master-lufs", "-14", "--true-peak-db", "-1",
        ]
        if analysis_cache is not None:
            arguments.extend(("--analysis-cache", str(analysis_cache)))
        result = djmix.main(arguments)
        if result:
            raise ExportError("The high-quality reference mix could not be rendered.")
