# Bundled command-line tools

The Windows installer keeps these programs as separate command-line executables. They retain their own licenses:

- **CPython 3.13** — Python Software Foundation License; https://www.python.org/downloads/
- **SongRec 0.7.4** — GNU GPL v3; https://github.com/marin-m/SongRec/tree/0.7.4
- **FFmpeg** — GNU LGPL v2.1+ / GPL components as reported by the bundled build; https://ffmpeg.org/legal.html
- **MP3Gain 1.6.2** — GNU LGPL; source and Windows downloads: https://mp3gain.sourceforge.net/download.php
- **yt-dlp** — The Unlicense plus bundled third-party notices; https://github.com/yt-dlp/yt-dlp
- Native libraries copied from **MSYS2 UCRT64** retain their respective licenses; package sources are available at https://packages.msys2.org/

CratePilot invokes these tools with argument arrays. It does not combine their code into CratePilot's Python package. See each upstream project for source, copyright, and complete license terms.
