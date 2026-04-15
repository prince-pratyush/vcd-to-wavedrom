"""Command-line entry point for vcd2wavedrom.

Orchestration
-------------
    parse args
        → open VCD source (file or stdin)
        → parser.parse()   → VCDModel
        → emitter.emit()   → WaveJSON dict
        → json.dump()      → stdout or --output file

All warnings go to stderr so stdout stays clean JSON.
VCDParseError / VCDEmitError cause a non-zero exit with a human-readable
message on stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings

from . import __version__
from .model import VCDError, VCDWarning
from .parser import parse
from .emitter import emit


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vcd2wavedrom",
        description=(
            "Convert a VCD simulation dump to WaveDrom WaveJSON.\n\n"
            "Reads FILE (or stdin if FILE is omitted) and writes JSON to\n"
            "stdout (or --output). All warnings are printed to stderr."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument(
        "file",
        metavar="FILE",
        nargs="?",
        help="Path to the VCD file.  Reads stdin if omitted.",
    )
    p.add_argument(
        "--signals", "-s",
        metavar="NAMES",
        help=(
            "Comma-separated signal names to include.  Each name is matched "
            "as a full hierarchical path or a dot-preceded suffix.  "
            "Example: -s clk,tb.data"
        ),
    )
    p.add_argument(
        "--period", "-p",
        metavar="N",
        type=int,
        help=(
            "Sampling period in VCD time units.  Signals are sampled at "
            "0, N, 2N, …  Default: event-driven (one tick per change timestamp)."
        ),
    )
    p.add_argument(
        "--group", "-g",
        action="store_true",
        help="Wrap signals in WaveJSON group arrays mirroring the VCD scope hierarchy.",
    )
    p.add_argument(
        "--output", "-o",
        metavar="PATH",
        help="Write JSON to PATH instead of stdout.",
    )
    p.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON with 2-space indentation.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Entry point.  Returns an exit code (0 = success, 1 = error)."""
    args = _build_arg_parser().parse_args(argv)

    # ── Validate --period ────────────────────────────────────────────────────
    if args.period is not None and args.period <= 0:
        print(
            f"vcd2wavedrom: error: --period must be a positive integer, "
            f"got {args.period}",
            file=sys.stderr,
        )
        return 1

    # ── Parse --signals into a list ──────────────────────────────────────────
    signal_filter: list[str] | None = None
    if args.signals:
        signal_filter = [s.strip() for s in args.signals.split(",") if s.strip()]

    # ── Open the input source ─────────────────────────────────────────────────
    in_fh = None
    if args.file:
        try:
            in_fh = open(args.file, "r")
        except OSError as exc:
            print(f"vcd2wavedrom: error: {exc}", file=sys.stderr)
            return 1

    # ── Parse + emit, capturing VCDWarnings ──────────────────────────────────
    result: dict
    caught_warnings: list
    try:
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always", VCDWarning)
            try:
                source = in_fh if in_fh is not None else sys.stdin
                model = parse(source)
                result = emit(
                    model,
                    signals=signal_filter,
                    period=args.period,
                    group=args.group,
                )
            except VCDError as exc:
                print(f"vcd2wavedrom: error: {exc}", file=sys.stderr)
                return 1
            except Exception as exc:
                print(f"vcd2wavedrom: unexpected error: {exc}", file=sys.stderr)
                return 1
    finally:
        if in_fh is not None:
            in_fh.close()

    # Print any warnings that were captured during parsing / emission
    for w in caught_warnings:
        print(f"vcd2wavedrom: warning: {w.message}", file=sys.stderr)

    # ── Serialise to JSON ─────────────────────────────────────────────────────
    indent = 2 if args.pretty else None
    try:
        if args.output:
            with open(args.output, "w") as out_fh:
                json.dump(result, out_fh, indent=indent)
                out_fh.write("\n")
        else:
            json.dump(result, sys.stdout, indent=indent)
            sys.stdout.write("\n")
    except OSError as exc:
        print(f"vcd2wavedrom: error writing output: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
