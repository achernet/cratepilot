#!/usr/bin/env python3
"""
Compatibility entry point for rendering DJ sets with djmix.py.

Keep this file beside djmix.py. It forwards every argument to:

    djmix.py render ...

Examples
========

Render an M3U playlist:

    ./djset.py trance.m3u -o trance-set.flac

Automatically choose the order:

    ./djset.py trance.m3u \
        --auto-order \
        --start "Opening Track.mp3" \
        -o trance-set.flac

Render explicit tracks with transition directives:

    ./djset.py \
        "Track 1.mp3" \
        -P16C4B12F0H0.55SB \
        "Track 2.mp3" \
        -P16C2B14F1H0.50SE \
        "Track 3.mp3" \
        -o trance-set.flac

All rendering options are documented by:

    ./djset.py --help
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Sequence

VERSION = "1.0.0"


class DjSetError(RuntimeError):
    """Raised when the sibling djmix.py implementation cannot be loaded."""


def load_djmix() -> ModuleType:
    """Load djmix.py from the same directory as this wrapper."""

    module_path = Path(__file__).resolve().with_name("djmix.py")
    if not module_path.is_file():
        raise DjSetError(
            f"Could not find {module_path}. Keep djset.py and djmix.py "
            "in the same directory."
        )

    spec = importlib.util.spec_from_file_location("djmix", module_path)
    if spec is None or spec.loader is None:
        raise DjSetError(f"Could not create an import specification for {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def extract_global_options(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """
    Move djmix global options before its ``render`` subcommand.

    djmix expects ``--log-level`` before ``render``. Users of this wrapper
    should not need to know that implementation detail.
    """

    global_options: list[str] = []
    render_options: list[str] = []
    index = 0

    while index < len(argv):
        argument = argv[index]

        if argument == "--log-level":
            if index + 1 >= len(argv):
                raise DjSetError("--log-level requires a value")
            global_options.extend((argument, argv[index + 1]))
            index += 2
            continue

        if argument.startswith("--log-level="):
            global_options.append(argument)
            index += 1
            continue

        render_options.append(argument)
        index += 1

    return global_options, render_options


def main(argv: Sequence[str] | None = None) -> int:
    """Forward this command line to the ``djmix render`` command."""

    arguments = list(sys.argv[1:] if argv is None else argv)

    if "--version" in arguments:
        print(f"djset.py {VERSION}")
        return 0

    djmix = load_djmix()
    global_options, render_options = extract_global_options(arguments)

    if not render_options:
        render_options = ["--help"]

    try:
        return int(djmix.main([*global_options, "render", *render_options]))
    except DjSetError:
        raise
    except Exception as exc:
        # djmix handles expected command-line errors itself. This preserves
        # an actionable message for unexpected loading or compatibility errors.
        raise DjSetError(f"djmix.py failed unexpectedly: {exc}") from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DjSetError as exc:
        print(f"djset.py: error: {exc}", file=sys.stderr)
        raise SystemExit(2)

