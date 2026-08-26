# Windows setup and first-booth handoff

## One-click install

1. Download [CratePilot-Setup-x64.exe](https://github.com/achernet/cratepilot/releases/latest/download/CratePilot-Setup-x64.exe).
2. Run it as your normal Windows user. The installer is per-user and does not require administrator access.
3. Leave **Launch CratePilot** selected, choose the music folder CratePilot may read, and work in the browser window it opens.

The installer contains an isolated Python 3.13 runtime, SongRec, FFmpeg/FFprobe, MP3Gain, yt-dlp, and all Python dependencies. Its `bin` directory is added to your user PATH. Check the installation at any time:

~~~powershell
cratepilot doctor
~~~

The GitHub release workflow builds SongRec 0.7.4 in an MSYS2 UCRT64 environment, assembles the native DLL closure, installs the CratePilot wheel into the bundled Python runtime, compiles a standard `setup.exe`, silently installs it on a clean Windows runner, and exercises every bundled command before publishing the release asset.

## Source install (development fallback)

Install Python 3.11 or newer and put `songrec`, `ffmpeg`, `ffprobe`, `mp3gain`, and `yt-dlp` on PATH. Then clone CratePilot and run:

~~~powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install ".[discovery]"
cratepilot doctor
cratepilot --library "D:\Music\Trance"
~~~

On future sessions, activate the environment and run only the last command. The setup.exe creates its own Start Menu and desktop launchers; the following is only for a source checkout:

~~~text
powershell.exe -NoExit -Command "& 'C:\path\to\cratepilot\.venv\Scripts\Activate.ps1'; cratepilot --library 'D:\Music\Trance'"
~~~

## Discover and curate

1. Open the **Seed** step and add a local track, public Spotify URL, or `Artist - Title` query.
2. Leave the initial safety limits at two hops, 150 canonical nodes, 30 review candidates, and eight strict drafts.
3. Inspect the graph and version-score explanations. Already-owned and duplicate identities do not increase readiness.
4. Prefer the safe Beatport/Bandcamp/source links. If you enable permissive acquisition, read the acknowledgement and approve the exact batch first.
5. Let CratePilot verify each acquired result. SongRec analyzes 11 random 12-second excerpts; files that do not reach a 6-of-11 Shazam-result consensus are rejected or quarantined.
6. Build smart crates with tags and analysis rules, then materialize them to M3U8 or send them to planning.

Spotify URLs require metadata credentials set as `CRATEPILOT_SPOTIFY_CLIENT_ID` and `CRATEPILOT_SPOTIFY_CLIENT_SECRET`. Spotify audio is never fetched.

## Compose

1. Analyze the folder. Originals are never renamed or altered.
2. Generate 1–30 First Booth — 45 min drafts (eight by default for discovery readiness).
3. Audition each transition and inspect BPM, Camelot movement, energy direction, and learned precedent.
4. Drag to reorder, lock non-negotiable tracks, replace weak neighbors, and undo freely.
5. Rehearse the chosen sequence before locking it.

## Export and verify

1. Export to a new empty staging folder.
2. Open current Rekordbox in Export mode and import playlist.m3u8.
3. Analyze tracks. Verify the first downbeat, BPM, and complete beat grid for every track.
4. Enter Hot Cues A/B/C and read mix-in/out and handoff notes from cue-sheet.html.
5. Confirm venue player and mixer models.
6. Format two reliable USB sticks as FAT32 and export from Rekordbox to both.
7. Use the current Rekordbox version so compatible Device Library and OneLibrary formats are produced where supported. See the [AlphaTheta compatibility notice](https://alphatheta.com/en/information/important-notice-for-customers-using-usb-devices-with-our-dj-equipment/).
8. Eject and reconnect both sticks. Verify playlist, artwork, waveforms, hot cues, and track loading.
9. Rehearse the locked set three times.
10. Keep reference-mix.flac on a separate phone or device as a safety copy.

CratePilot’s cue suggestions are preparation aids. Venue compatibility and Rekordbox beat grids must be verified by the DJ.
