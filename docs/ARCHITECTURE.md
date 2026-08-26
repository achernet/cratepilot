# Architecture

~~~text
Public browser                       Local browser
  six sanitized records               per-launch token
  deterministic scorer                127.0.0.1 only
  Web Audio approximation                    │
  no upload/API route                        ▼
         │                            FastAPI + job runner
         │                              │       │
         ▼                              ▼       ▼
React/TypeScript UI              SQLite store  validated library roots
                                        │       │
                                        ▼       ▼
                                  beam planner  analysis cache
                                        │       │
                                        └──┬────┘
                                           ▼
                                   DSP renderer/exporter
                                           │
                                           ▼
                               M3U8 + cues + FLAC + checksums
~~~

## Stable models

- TrackAnalysisV1 holds display metadata, duration, BPM/key/Camelot, energy, audible bounds, cue suggestions, and intro/outro feature vectors.
- TransitionPlanV1 holds the total and component scores, tempo factor, phrase/crossfade bars, bass/filter/fade parameters, explanation, and warning.
- SetPlanV1 holds the preset, ordered IDs, locks, curve, transitions, warnings, and schema/model versions.
- ExportManifestV1 records generated paths and hashes, duration, mastering settings, and the Rekordbox checklist.

## Trust boundaries

Public assets cannot receive or process visitor audio. Local endpoints require a random token and reject foreign origins. Every analysis path must resolve beneath the selected library root. Exports copy rather than move source tracks, never contain absolute playlist paths, and refuse a non-empty destination.

The private workshop’s .djlearn, cached source audio, recognition data, and local filenames are intentionally absent.
