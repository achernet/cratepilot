# Architecture

~~~text
Public browser                         Local browser
  sanitized graph fixture               per-launch token / 127.0.0.1
  deterministic scorer                  selected library roots
  visualizer, no provider calls                  │
         │                                        ▼
         ▼                              FastAPI + background jobs
React/TypeScript UI                              │
                                  ┌───────────────┼───────────────┐
                                  ▼               ▼               ▼
                           provider adapters  SQLite catalog  analysis cache
                                  │          identities + edges      │
                                  ▼               │                  ▼
                         reviewed acquisition     └──────────► beam planner
                         immutable source                    strict readiness
                         verified derivative                       │
                                  └───────────────┬─────────────────┘
                                                  ▼
                                          DSP renderer/exporter
                                                  ▼
                                     M3U8 + cues + FLAC + checksums
~~~

## Core Python modules

| Module | Responsibility | Does not do |
| --- | --- | --- |
| `analysis.py` | Scans supported audio and runs parallel per-track DSP, preserving input order. | Identify songs or choose a set order. |
| `api.py` | Defines the token-protected loopback API and connects requests to background jobs. | Host a public service or trust arbitrary paths. |
| `cli.py` / `__main__.py` | Parse commands and compose the same local services used by the UI. | Contain discovery, DSP, or planning algorithms. |
| `identity.py` / `models.py` | Define canonical IDs and versioned records shared by JSON, SQLite, and services. | Perform I/O or provider calls. |
| `providers.py` | Resolve Spotify metadata, Shazam local-seed pages, Deezer metadata similarity, and yt-dlp search results. | Download or approve audio. |
| `discovery.py` | Breadth-first similarity expansion, canonical deduplication, provenance edges, candidate ranking, and readiness checks. | Acquire audio automatically. |
| `acquisition.py` / `recognition.py` | Enforce explicit approval, acquire candidates, verify SongRec consensus, enrich years, and quarantine mismatches. | Run unless the local policy acknowledgement and batch approval are present. |
| `planner.py` | Score transitions and generate deterministic bounded-beam drafts from analyzed tracks. | Silently override locks or warnings. |
| `crates.py` | Recompute smart-crate membership from saved rules and manual overrides, then optionally write M3U8. | Move or modify source tracks. |
| `exporter.py` | Stage copied tracks, cues, checksums, playlists, manifests, and an optional reference mix. | Write Pioneer/AlphaTheta databases. |
| `storage.py` / `jobs.py` | Persist records, progress, logs, and cooperative cancellation state in SQLite. | Define product policy or scoring. |
| `file_picker.py` / `doctor.py` | Provide platform file selection/import boundaries and native dependency diagnostics. | Analyze audio. |
| `youtube_scorer.py` | Explainably rank source metadata for human review. | Download a result. |

## Stable models

- TrackAnalysisV1 holds display metadata, duration, BPM/key/Camelot, energy, audible bounds, cue suggestions, and intro/outro feature vectors.
- TransitionPlanV1 holds the total and component scores, tempo factor, phrase/crossfade bars, bass/filter/fade parameters, explanation, and warning.
- SetPlanV1 holds the preset, ordered IDs, locks, curve, transitions, warnings, and schema/model versions.
- ExportManifestV1 records generated paths and hashes, duration, mastering settings, and the Rekordbox checklist.
- CatalogTrackV2 holds canonical identity, external identifiers, provenance, verification state, local assets, year evidence, tags, and the linked analysis.
- DiscoveryEdgeV1 and DiscoverySessionV1 persist bounded, resumable graph traversal and readiness results.
- AcquisitionCandidateV1 and AcquisitionJobV1 make ranking, approval, attempts, verification, quarantine, and generated assets auditable.
- SmartCrateV1 stores a saved catalog query (a “smart playlist” for a DJ library), explicit inclusion/exclusion, ordering, and the last materialized membership. Materialization changes the saved view, never the audio files.

## Trust boundaries

Public assets cannot receive or process visitor audio. Local endpoints require a random token and reject foreign origins. Every analysis path must resolve beneath the selected library root. Exports copy rather than move source tracks, never contain absolute playlist paths, and refuse a non-empty destination.

Acquisition uses subprocess argument arrays, temporary staging, content hashes, atomic promotion, and exact managed-asset streaming. The external `songrec recognize -j` CLI runs against eleven random 12-second samples; six matching Shazam results are required. A mismatch is quarantined and the next approved ranked version is tried. Keeping recognition out of the Python process avoids the Python 3.13 incompatibility in `shazamio`.

The release pipeline builds SongRec in MSYS2 UCRT64, bundles the complete native dependency closure and an isolated Python 3.13 runtime, then compiles and silently installs `CratePilot-Setup-x64.exe` on a clean Windows runner. The installed `cratepilot doctor` command is the acceptance gate for Python, SongRec, FFmpeg/FFprobe, MP3Gain, and yt-dlp.

The private workshop’s .djlearn, cached source audio, recognition data, and local filenames are intentionally absent. Spotify credentials are supplied through environment variables and never enter public builds.
