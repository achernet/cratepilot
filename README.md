# CratePilot

**Build a set you can trust before you enter the booth.**

CratePilot is a provenance-aware music discovery and explainable DJ-set planner with two deliberately separate modes:

- The public recruiter demo is anonymous and stateless beyond browser storage. It simulates seed expansion, provenance review, smart-crate readiness, editable drafts, and a signal visualizer using six credited records.
- The local application discovers from private files, public Spotify URLs, or manual searches; reviews sources; verifies acquired audio; analyzes a library; plans 45-minute sets; and stages a non-destructive Rekordbox package.

The recommendation model is example-based transition learning combined with DSP heuristics—not an autonomous “AI DJ.” The workshop that preceded this extraction has 99 passing tests and 7 passing subtests; this repository adds product-specific planner, schema, storage, security, and export coverage.

## Run locally

On Windows 10/11, download the [one-click installer](https://github.com/achernet/cratepilot/releases/latest/download/CratePilot-Setup-x64.exe). It bundles an isolated Python 3.13 runtime plus `songrec`, FFmpeg/FFprobe, `mp3gain`, and `yt-dlp`, adds CratePilot's command shims to the user PATH, and runs `cratepilot doctor` before launch.

For a source installation, Python 3.11 or newer plus `songrec`, `ffmpeg`, `ffprobe`, `mp3gain`, and `yt-dlp` must be on PATH:

~~~powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[discovery]"
cratepilot doctor
cratepilot --library "D:\Music\Trance"
~~~

CratePilot opens at 127.0.0.1 with a per-launch session token. Analysis and rendered data live in the platform user-data/cache directories. Source audio is read-only.

The local interface uses native file dialogs for seeds. A file selected outside the active library is copied into it without changing the original. Long analysis, discovery, planning, acquisition, and export jobs show progress and timestamped logs and can be cancelled at safe checkpoints. Before planning a very large library, use the optional M3U/M3U8 filter to restrict the candidate pool; playlist entries outside the selected library are ignored.

Useful automation commands:

~~~text
cratepilot analyze PATH...
cratepilot plan --preset first-booth-45 --library PATH
cratepilot render PLAN --library-json ANALYSIS --output reference-mix.flac
cratepilot export PLAN --library-json ANALYSIS --output EXPORT_FOLDER
cratepilot discover "Artist - Title" --target-drafts 8
cratepilot discover --spotify https://open.spotify.com/track/...
cratepilot acquire                         # inspect the queue
cratepilot acquire ID...                   # approve only
cratepilot acquire ID... --run             # run an approved local batch
cratepilot catalog
cratepilot crate --name Peak --rule energy:gte:75
cratepilot crate --id CRATE_ID --output peak.m3u8
cratepilot doctor                         # verify every native command
~~~

See [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md) for the complete Windows and Rekordbox handoff.

## What the planner optimizes

Bounded beam search returns 1–30 deterministic drafts. Its objective is 70% mean transition compatibility, 20% fit to the requested energy curve, 5% final-duration fit after overlaps, and 5% artist spacing. Transition scores are cached within a planning run, and progress is reported at every beam depth so large libraries remain observable and cancellable. Strict readiness additionally requires 42–48 minutes, no transition warning, at least 0.55 mean compatibility, energy error no greater than 20 points, artist spacing, and no more than 0.75 Jaccard overlap with another counted draft.

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

Local acquisition is off by default. Safe mode exposes Beatport, Bandcamp, and YouTube source links. Enabling automation requires a versioned standing acknowledgement saved in the local SQLite database and an explicit review batch of at most 30 candidates. Downloaded sources are content-addressed and immutable; verified 320 kbps DJ derivatives are separate files. SongRec invokes Shazam recognition through its external JSON CLI, so CratePilot supports Python 3.13+ without importing `shazamio`. Identity mismatches are quarantined rather than promoted.

Spotify is used for metadata and links only. Configure `CRATEPILOT_SPOTIFY_CLIENT_ID` and `CRATEPILOT_SPOTIFY_CLIENT_SECRET` for public track or playlist resolution; no Spotify-hosted audio is downloaded or analyzed.

## First-booth checklist

1. Confirm the venue’s exact players and mixer.
2. Import the generated M3U8 into the latest Rekordbox and verify every beat grid and cue.
3. Export through Rekordbox to two separately formatted FAT32 USB sticks.
4. Eject, reconnect, and test both sticks.
5. Rehearse the locked set three times.
6. Keep the reference mix on a separate device as a safety copy.

## License

CratePilot source is MIT licensed. Demo track metadata remains attributed to its respective creators; source recordings are not included.
