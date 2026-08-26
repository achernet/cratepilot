# Windows setup and first-booth handoff

## Install once

1. Install Python 3.11 or newer from python.org and enable **Add Python to PATH**.
2. Install FFmpeg and confirm ffmpeg -version works in PowerShell.
3. Download or clone CratePilot, open PowerShell in its folder, and run:

~~~powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install .
cratepilot --library "D:\Music\Trance"
~~~

On future sessions, activate the environment and run only the last command. A desktop shortcut may target:

~~~text
powershell.exe -NoExit -Command "& 'C:\path\to\cratepilot\.venv\Scripts\Activate.ps1'; cratepilot --library 'D:\Music\Trance'"
~~~

## Compose

1. Analyze the folder. Originals are never renamed or altered.
2. Generate the three First Booth — 45 min drafts.
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
