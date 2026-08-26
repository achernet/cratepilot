from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from pathlib import Path

from platformdirs import user_cache_path

from . import __version__
from .acquisition import AcquisitionService
from .analysis import analyze_paths, scan_library
from .crates import materialize_crate, write_m3u8
from .discovery import Catalog, DiscoveryService
from .exporter import write_rekordbox_package
from .identity import stable_id
from .models import SmartCrateV1, plan_from_dict, to_dict, track_from_dict, write_json
from .planner import generate_drafts
from .providers import ProviderTrack, ShazamRelatedProvider, SpotifyMetadataProvider, YtDlpSearchProvider
from .recognition import ShazamMusicBrainzVerifier
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
    plan.add_argument("--count", type=int, default=3, choices=range(1, 31))
    render = subparsers.add_parser("render", help="Render a plan to a mastered reference mix")
    render.add_argument("plan", type=Path)
    render.add_argument("--library-json", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    export = subparsers.add_parser("export", help="Prepare a non-destructive Rekordbox package")
    export.add_argument("plan", type=Path)
    export.add_argument("--library-json", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--skip-reference-mix", action="store_true")
    discover = subparsers.add_parser("discover", help="Build a provenance-aware discovery and acquisition queue")
    discover.add_argument("seeds", nargs="*", help="Local audio paths or 'Artist - Title' searches")
    discover.add_argument("--spotify", action="append", default=[], help="Public Spotify track or playlist URL")
    discover.add_argument("--target-drafts", type=int, default=8, choices=range(1, 31))
    discover.add_argument("--max-depth", type=int, default=2)
    discover.add_argument("--max-nodes", type=int, default=150)
    discover.add_argument("--result-count", type=int, default=30)
    acquire = subparsers.add_parser("acquire", help="Review or execute a local acquisition batch")
    acquire.add_argument("candidate_ids", nargs="*")
    acquire.add_argument("--acknowledge-local-responsibility", action="store_true")
    acquire.add_argument("--revoke", action="store_true")
    acquire.add_argument("--run", action="store_true")
    catalog = subparsers.add_parser("catalog", help="Inspect or correct canonical catalog identities")
    catalog.add_argument("--merge", nargs=2, metavar=("TARGET", "SOURCE"))
    crate = subparsers.add_parser("crate", help="Create a smart crate or export it as M3U8")
    crate.add_argument("--name")
    crate.add_argument("--rule", action="append", default=[], help="Rule as field:operator:value")
    crate.add_argument("--order-by", default="energy")
    crate.add_argument("--descending", action="store_true")
    crate.add_argument("--id")
    crate.add_argument("--output", type=Path)
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
        store = Store()
        store.save_tracks(tracks)
        Catalog(store).import_analyses(tracks)
        payload = {"schema_version": 1, "tracks": [to_dict(track) for track in tracks]}
        if args.output:
            write_json(args.output, payload)
        else:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if args.command == "plan":
        paths = scan_library(args.library)
        tracks = analyze_paths(paths, cache_directory=_cache())
        drafts = generate_drafts(tracks, count=args.count)
        write_json(args.output, {"schema_version": 1, "tracks": [to_dict(track) for track in tracks], "plans": drafts})
        print(f"Wrote {len(drafts)} drafts to {args.output}")
        return 0
    if args.command == "discover":
        store = Store()
        seeds: list[ProviderTrack] = []
        for value in args.seeds:
            path = Path(value).expanduser()
            if path.is_file():
                tracks = analyze_paths([path.resolve()], cache_directory=_cache())
                store.save_tracks(tracks)
                Catalog(store).import_analyses(tracks)
                seeds.extend(
                    ProviderTrack(item.artist, item.title, "local", external_id=item.id, evidence={"path": item.path})
                    for item in tracks
                )
            elif " - " in value:
                artist, title = value.split(" - ", 1)
                seeds.append(ProviderTrack(artist.strip(), title.strip(), "manual"))
            else:
                parser.error(f"discovery seed must be an audio path or 'Artist - Title': {value}")
        for url in args.spotify:
            seeds.extend(SpotifyMetadataProvider().resolve(url))
        if not seeds:
            parser.error("discover requires at least one seed or --spotify URL")
        service = DiscoveryService(store, similarity=ShazamRelatedProvider(), video_search=YtDlpSearchProvider())
        session = service.create_session(
            seeds, max_depth=args.max_depth, max_nodes=args.max_nodes,
            readiness_target=args.target_drafts, result_count=args.result_count,
        )
        print(json.dumps(to_dict(service.run(session.id)), indent=2, ensure_ascii=False))
        return 0
    if args.command == "acquire":
        store = Store()
        service = AcquisitionService(store, verifier=ShazamMusicBrainzVerifier())
        if args.revoke:
            service.acknowledge(False)
            print("Permissive acquisition disabled.")
            return 0
        if args.acknowledge_local_responsibility:
            service.acknowledge(True)
        if not args.candidate_ids:
            print(json.dumps({"acknowledged": service.is_acknowledged(), "candidates": [to_dict(x) for x in store.candidates()]}, indent=2))
            return 0
        job = service.approve(args.candidate_ids)
        value = service.run(job) if args.run else job
        print(json.dumps(to_dict(value), indent=2, ensure_ascii=False))
        return 0
    if args.command == "catalog":
        store = Store()
        if args.merge:
            value = Catalog(store).merge(*args.merge)
            print(json.dumps(to_dict(value), indent=2, ensure_ascii=False))
        else:
            print(json.dumps({"tracks": [to_dict(item) for item in store.catalog_tracks()]}, indent=2, ensure_ascii=False))
        return 0
    if args.command == "crate":
        store = Store()
        if args.id and args.output:
            value = store.crate(args.id)
            if value is None:
                parser.error(f"unknown smart crate: {args.id}")
            write_m3u8(args.output, value, store.catalog_tracks())
            print(f"Wrote {args.output}")
            return 0
        if not args.name:
            print(json.dumps({"crates": [to_dict(item) for item in store.crates()]}, indent=2, ensure_ascii=False))
            return 0
        rules = []
        for rule in args.rule:
            try:
                field, operator, raw_value = rule.split(":", 2)
            except ValueError:
                parser.error(f"invalid rule '{rule}'; expected field:operator:value")
            value: object = raw_value
            if operator == "between":
                value = [float(item) for item in raw_value.split(",", 1)]
            elif operator in {"gte", "lte"}:
                value = float(raw_value)
            rules.append({"field": field, "operator": operator, "value": value})
        value = SmartCrateV1(
            id=stable_id("crate", args.name), name=args.name, rules=tuple(rules),
            order_by=args.order_by, descending=args.descending,
        )
        value = materialize_crate(value, store.catalog_tracks(), store.tracks())
        store.save_crate(value)
        print(json.dumps(to_dict(value), indent=2, ensure_ascii=False))
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
