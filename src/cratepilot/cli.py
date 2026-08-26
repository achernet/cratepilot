from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from pathlib import Path

from platformdirs import user_cache_path

from . import __version__
from .analysis import analyze_paths, scan_library
from .exporter import write_rekordbox_package
from .models import plan_from_dict, to_dict, track_from_dict, write_json
from .planner import generate_drafts
from .storage import Store


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cratepilot", description="Build an explainable DJ set and prepare it for Rekordbox.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--library", type=Path, help="Music folder to open in the local CratePilot interface")
    subparsers = parser.add_subparsers(dest="command")
    analyze = subparsers.add_parser("analyze", help="Analyze tracks or a complete folder")
    analyze.add_argument("paths", nargs="+", type=Path)
    analyze.add_argument("--output", type=Path)
    plan = subparsers.add_parser("plan", help="Generate three first-booth drafts")
    plan.add_argument("--preset", default="first-booth-45", choices=("first-booth-45",))
    plan.add_argument("--library", type=Path, required=True)
    plan.add_argument("--output", type=Path, default=Path("cratepilot-plans.json"))
    render = subparsers.add_parser("render", help="Render a plan to a mastered reference mix")
    render.add_argument("plan", type=Path)
    render.add_argument("--library-json", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    export = subparsers.add_parser("export", help="Prepare a non-destructive Rekordbox package")
    export.add_argument("plan", type=Path)
    export.add_argument("--library-json", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--skip-reference-mix", action="store_true")
    return parser


def _cache() -> Path:
    return user_cache_path("CratePilot", "Chernetz") / "analysis"


def _serve(library: Path) -> int:
    try:
        import uvicorn

        from .api import create_app
    except ImportError:
        print("CratePilot's local interface requires the 'local' dependencies. Reinstall the complete package.", file=sys.stderr)
        return 2
    library = library.expanduser().resolve()
    if not library.is_dir():
        print(f"Music library does not exist: {library}", file=sys.stderr)
        return 2
    web_directory = Path(__file__).resolve().parent / "web"
    app = create_app(library, web_directory=web_directory)
    token = app.state.cratepilot.token
    url = f"http://127.0.0.1:8765/?local=1&token={token}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"CratePilot is opening at {url}")
    print("Press Ctrl+C to stop it. Your music never leaves this computer.")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        if args.library is None:
            parser.error("provide --library PATH to open the local interface")
        return _serve(args.library)
    if args.command == "analyze":
        paths = []
        for value in args.paths:
            paths.extend(scan_library(value) if value.is_dir() else [value])
        tracks = analyze_paths(paths, cache_directory=_cache())
        Store().save_tracks(tracks)
        payload = {"schema_version": 1, "tracks": [to_dict(track) for track in tracks]}
        if args.output:
            write_json(args.output, payload)
        else:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if args.command == "plan":
        paths = scan_library(args.library)
        tracks = analyze_paths(paths, cache_directory=_cache())
        drafts = generate_drafts(tracks)
        write_json(args.output, {"schema_version": 1, "tracks": [to_dict(track) for track in tracks], "plans": drafts})
        print(f"Wrote three drafts to {args.output}")
        return 0
    payload = json.loads(args.plan.read_text(encoding="utf-8"))
    plan_value = payload["plan"] if "plan" in payload else payload
    plan = plan_from_dict(plan_value)
    library_payload = json.loads(args.library_json.read_text(encoding="utf-8"))
    library = {track.id: track for track in (track_from_dict(item) for item in library_payload["tracks"])}
    if args.command == "render":
        with __import__("tempfile").TemporaryDirectory(prefix="cratepilot-render-export-") as temporary:
            manifest = write_rekordbox_package(plan, library, Path(temporary), render_reference=True, analysis_cache=_cache())
            reference = Path(manifest.output_directory) / "reference-mix.flac"
            args.output.parent.mkdir(parents=True, exist_ok=True)
            __import__("shutil").copy2(reference, args.output)
        print(f"Rendered {args.output}")
        return 0
    if args.command == "export":
        manifest = write_rekordbox_package(
            plan, library, args.output, render_reference=not args.skip_reference_mix, analysis_cache=_cache()
        )
        print(json.dumps(to_dict(manifest), indent=2, ensure_ascii=False))
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
