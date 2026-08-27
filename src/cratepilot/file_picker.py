from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from .analysis import SUPPORTED_EXTENSIONS


class FilePickerError(RuntimeError):
    pass


def _windows_picker(initial_directory: Path, extensions: Iterable[str]) -> Path | None:
    patterns = ";".join(f"*{extension}" for extension in sorted(extensions))
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.InitialDirectory = $args[0]
$dialog.Filter = "Audio files ($args[1])|$args[1]|All files (*.*)|*.*"
$dialog.Multiselect = $false
$dialog.CheckFileExists = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  Write-Output $dialog.FileName
}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", script, str(initial_directory), patterns],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode:
        raise FilePickerError(result.stderr.strip() or "Windows could not open the audio file picker.")
    selected = result.stdout.strip()
    return Path(selected) if selected else None


def _tk_picker(initial_directory: Path, extensions: Iterable[str]) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise FilePickerError("No desktop file picker is available on this system.") from exc
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    patterns = " ".join(f"*{extension}" for extension in sorted(extensions))
    try:
        selected = filedialog.askopenfilename(
            initialdir=str(initial_directory),
            title="Choose music for CratePilot",
            filetypes=(("Audio files", patterns), ("All files", "*.*")),
        )
    finally:
        root.destroy()
    return Path(selected) if selected else None


def choose_audio_file(initial_directory: Path) -> Path | None:
    initial_directory = initial_directory.expanduser().resolve()
    if os.name == "nt":
        return _windows_picker(initial_directory, SUPPORTED_EXTENSIONS)
    return _tk_picker(initial_directory, SUPPORTED_EXTENSIONS)


def choose_playlist_file(initial_directory: Path) -> Path | None:
    initial_directory = initial_directory.expanduser().resolve()
    if os.name == "nt":
        script = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.InitialDirectory = $args[0]
$dialog.Filter = "M3U playlists (*.m3u;*.m3u8)|*.m3u;*.m3u8|All files (*.*)|*.*"
$dialog.Multiselect = $false
$dialog.CheckFileExists = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  Write-Output $dialog.FileName
}
"""
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script, str(initial_directory)],
            check=False, capture_output=True, text=True, timeout=300,
        )
        if result.returncode:
            raise FilePickerError(result.stderr.strip() or "Windows could not open the playlist picker.")
        selected = result.stdout.strip()
        return Path(selected) if selected else None
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise FilePickerError("No desktop file picker is available on this system.") from exc
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askopenfilename(
            initialdir=str(initial_directory), title="Choose an M3U playlist",
            filetypes=(("M3U playlists", "*.m3u *.m3u8"), ("All files", "*.*")),
        )
    finally:
        root.destroy()
    return Path(selected) if selected else None


def _safe_component(value: str) -> str:
    forbidden = '<>:"/\\|?*' + "".join(chr(value) for value in range(32))
    cleaned = "".join("_" if character in forbidden else character for character in value).strip(" .")
    return cleaned[:180] or "Imported track"


def import_into_library(source: Path, library_root: Path) -> tuple[Path, bool]:
    source = source.expanduser().resolve()
    library_root = library_root.expanduser().resolve()
    if not source.is_file():
        raise FilePickerError("The selected audio file no longer exists.")
    if source.suffix.casefold() not in SUPPORTED_EXTENSIONS:
        raise FilePickerError(f"Unsupported audio type: {source.suffix or 'no file extension'}")
    try:
        source.relative_to(library_root)
        return source, False
    except ValueError:
        pass

    library_root.mkdir(parents=True, exist_ok=True)
    stem = _safe_component(source.stem)
    suffix = source.suffix.casefold()
    target = library_root / f"{stem}{suffix}"
    copy_number = 2
    while target.exists():
        target = library_root / f"{stem} ({copy_number}){suffix}"
        copy_number += 1

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".cratepilot-import-", suffix=suffix, dir=library_root, delete=False) as staged:
            temporary_path = Path(staged.name)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, staged, length=1024 * 1024)
        shutil.copystat(source, temporary_path)
        os.replace(temporary_path, target)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise FilePickerError(f"Could not copy the selected file into the music library: {exc}") from exc
    return target.resolve(), True
