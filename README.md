# CratePilot

**Build a set you can trust before you enter the booth.**

CratePilot is an explainable DJ-set planner with two deliberately separate modes:

- The public recruiter demo is anonymous and stateless beyond browser storage. It has six credited demo records, editable ranked drafts, transition explanations, an energy path, and a lightweight Web Audio audition.
- The local application analyzes private MP3, FLAC, WAV, and M4A files, plans a 45-minute set, renders phrase-aware audio, and stages a non-destructive Rekordbox import package.

The recommendation model is example-based transition learning combined with DSP heuristics—not an autonomous “AI DJ.” The workshop that preceded this extraction has 99 passing tests and 7 passing subtests; this repository adds product-specific planner, schema, storage, security, and export coverage.

## Run locally

Requirements: Python 3.11 or newer and FFmpeg on PATH.

~~~powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
cratepilot --library "D:\Music\Trance"
~~~

CratePilot opens at 127.0.0.1 with a per-launch session token. Analysis and rendered data live in the platform user-data/cache directories. Source audio is read-only.

Useful automation commands:

~~~text
cratepilot analyze PATH...
cratepilot plan --preset first-booth-45 --library PATH
cratepilot render PLAN --library-json ANALYSIS --output reference-mix.flac
cratepilot export PLAN --library-json ANALYSIS --output EXPORT_FOLDER
~~~

See [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md) for the complete Windows and Rekordbox handoff.

## What the planner optimizes

Bounded beam search returns three deterministic drafts. Its objective is 70% mean transition compatibility, 20% fit to the requested energy curve, 5% final-duration fit after overlaps, and 5% artist spacing.

Transition compatibility explains tempo, Camelot harmony, energy, low-end balance, timbre, rhythm, and the learned-neighbor contribution. Locked tracks and user ordering are hard constraints. Manual changes are warned on, never silently undone.

## Rekordbox package

Each export contains sanitized numbered track copies, relative playlist.m3u8, versioned set-plan.json, transition-notes.csv, printable cue-sheet.html, checksums, an attribution manifest, and optionally reference-mix.flac targeted to −14 LUFS and ≤−1 dBTP.

CratePilot does not mutate Pioneer/AlphaTheta databases. Import the M3U8 into current Rekordbox, verify beat grids and suggested cues, then let Rekordbox export two tested FAT32 USB drives.

## Development

~~~bash
python -m pytest
pnpm install
pnpm lint
pnpm build
~~~

The Python package is under src/cratepilot; the shared product presentation is the Next/React app under app. The wheel includes a dependency-free localhost interface so cratepilot --library PATH remains one command after installation.

## Privacy and provenance

The old .djlearn workspace, cached audio, personal filenames, and absolute music paths are excluded from this project. No public route accepts uploads. Public assets contain the social preview, sanitized metadata, and code only; the browser audition is synthesized rather than redistributed source audio. See [ATTRIBUTIONS.md](ATTRIBUTIONS.md).

## First-booth checklist

1. Confirm the venue’s exact players and mixer.
2. Import the generated M3U8 into the latest Rekordbox and verify every beat grid and cue.
3. Export through Rekordbox to two separately formatted FAT32 USB sticks.
4. Eject, reconnect, and test both sticks.
5. Rehearse the locked set three times.
6. Keep the reference mix on a separate device as a safety copy.

## License

CratePilot source is MIT licensed. Demo track metadata remains attributed to its respective creators; source recordings are not included.
