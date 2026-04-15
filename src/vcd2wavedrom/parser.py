"""VCD tokeniser and two-phase parser.

Phase 1  (_parse_header)  reads everything before ``$enddefinitions $end``.
         Builds the signal registry: scope hierarchy, variable declarations,
         and metadata.  Returns a :class:`~vcd2wavedrom.model.VCDModel` whose
         Signal.changes lists are all empty.

Phase 2  (_parse_body)    streams everything after ``$enddefinitions $end``.
         Appends ``(time, value)`` pairs to each Signal's change list.
         Mutates the model in-place.

The two phases share a single lazy token iterator so the file is read only
once and never fully buffered.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Iterator, TextIO

from .model import Signal, VCDModel, VCDParseError, VCDWarning


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

def tokenise(stream: TextIO) -> Iterator[str]:
    """Yield whitespace-delimited tokens from *stream* one at a time.

    VCD is not line-oriented — keywords and values can span lines freely.
    Splitting on whitespace and yielding individual tokens is sufficient.
    """
    for line in stream:
        yield from line.split()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _expect(tokens: Iterator[str], context: str) -> str:
    """Return the next token, raising :exc:`VCDParseError` on EOF."""
    try:
        return next(tokens)
    except StopIteration:
        raise VCDParseError(f"Unexpected end of file while parsing {context}")


def _collect_block(tokens: Iterator[str]) -> str:
    """Collect tokens until ``$end`` and return them as a single string."""
    parts: list[str] = []
    for tok in tokens:
        if tok == "$end":
            break
        parts.append(tok)
    return " ".join(parts).strip()


def _skip_to_end(tokens: Iterator[str]) -> None:
    """Discard all tokens up to and including the next ``$end``."""
    for tok in tokens:
        if tok == "$end":
            return


# ---------------------------------------------------------------------------
# Phase 1 — Header
# ---------------------------------------------------------------------------

def _parse_var(
    tokens: Iterator[str],
    model: VCDModel,
    scope_stack: list[str],
) -> None:
    """Parse one ``$var … $end`` declaration and register the signal.

    Format (IEEE 1800-2023 §21.7.2.1):
        $var  var_type  size  identifier_code  reference  [$bus_range]  $end

    ``identifier_code`` is one or more printable ASCII chars (0x21–0x7e).
    ``reference`` may include an optional bit-select suffix ``[msb:lsb]``
    either attached (``i[31:0]``) or as a separate token (``i [31:0]``).
    """
    var_type = _expect(tokens, "$var type")

    width_str = _expect(tokens, "$var width")
    try:
        width = int(width_str)
    except ValueError:
        raise VCDParseError(f"$var width is not an integer: {width_str!r}")

    code = _expect(tokens, "$var identifier code")

    # Warn if any code character is outside the printable-ASCII range.
    for ch in code:
        if not (0x21 <= ord(ch) <= 0x7E):
            warnings.warn(
                f"Identifier code {code!r} contains out-of-range character "
                f"{ch!r} (0x{ord(ch):02X}); IEEE 1800-2023 §21.7.2.1",
                VCDWarning,
                stacklevel=4,
            )

    raw_name = _expect(tokens, "$var reference name")

    # Strip a bus-range suffix attached directly to the name: "i[31:0]" → "i"
    if "[" in raw_name:
        raw_name = raw_name[: raw_name.index("[")]

    # Discard any remaining tokens in the declaration (e.g. separate "[31:0]")
    _skip_to_end(tokens)

    # Assemble the full hierarchical path.
    path = ".".join(scope_stack + [raw_name]) if scope_stack else raw_name

    # Create or reuse an existing Signal for this path.
    if path in model.signals:
        sig = model.signals[path]
    else:
        sig = Signal(path=path, code=code, width=width, kind=var_type)
        model.signals[path] = sig

    # Register in the code → [Signal, …] lookup (handles aliasing).
    model.codes.setdefault(code, []).append(sig)


def _parse_header(tokens: Iterator[str]) -> VCDModel:
    """Phase 1: consume header tokens up to ``$enddefinitions $end``.

    Returns a :class:`~vcd2wavedrom.model.VCDModel` with all signals
    registered but all ``Signal.changes`` lists empty.  The iterator is left
    positioned immediately after the ``$end`` that closes
    ``$enddefinitions``, ready for Phase 2.
    """
    model = VCDModel()
    scope_stack: list[str] = []
    found_enddefs = False

    for token in tokens:
        if token == "$date":
            model.date = _collect_block(tokens)

        elif token == "$version":
            model.version = _collect_block(tokens)

        elif token == "$timescale":
            model.timescale = _collect_block(tokens)

        elif token == "$scope":
            _scope_type = _expect(tokens, "$scope type")
            scope_name = _expect(tokens, "$scope identifier")
            closer = _expect(tokens, "$scope closing $end")
            if closer != "$end":
                raise VCDParseError(
                    f"Expected '$end' after $scope identifier, got {closer!r}"
                )
            scope_stack.append(scope_name)

        elif token == "$upscope":
            closer = _expect(tokens, "$upscope $end")
            if closer != "$end":
                raise VCDParseError(
                    f"Expected '$end' after $upscope, got {closer!r}"
                )
            if scope_stack:
                scope_stack.pop()

        elif token == "$var":
            _parse_var(tokens, model, scope_stack)

        elif token == "$comment":
            _skip_to_end(tokens)

        elif token == "$enddefinitions":
            closer = _expect(tokens, "$enddefinitions $end")
            if closer != "$end":
                raise VCDParseError(
                    f"Expected '$end' after $enddefinitions, got {closer!r}"
                )
            found_enddefs = True
            break

        elif token.startswith("$"):
            warnings.warn(
                f"Unknown header keyword {token!r}; skipping block",
                VCDWarning,
                stacklevel=3,
            )
            _skip_to_end(tokens)

        # Non-keyword tokens before $enddefinitions are silently ignored
        # (some simulators emit bare text outside blocks).

    if not found_enddefs:
        warnings.warn(
            "VCD file has no $enddefinitions; simulation body may be missing",
            VCDWarning,
            stacklevel=3,
        )

    return model


# ---------------------------------------------------------------------------
# Phase 2 — Simulation body
# ---------------------------------------------------------------------------

def _apply_change(
    model: VCDModel,
    code: str,
    value: str,
    time: int,
) -> None:
    """Append ``(time, value)`` to every signal registered under *code*."""
    if code not in model.codes:
        warnings.warn(
            f"Value change for undeclared identifier code {code!r} "
            f"at time {time}; ignoring",
            VCDWarning,
            stacklevel=4,
        )
        return
    for sig in model.codes[code]:
        sig.changes.append((time, value))


def _parse_body(tokens: Iterator[str], model: VCDModel) -> None:
    """Phase 2: stream simulation body, mutating *model* in-place.

    Recognised constructs
    ---------------------
    ``#<N>``
        Advance current simulation time to *N*.
    ``0code`` / ``1code`` / ``xcode`` / ``zcode``
        Scalar value change (value char + identifier code, no whitespace).
    ``b<bits> <code>``  /  ``B<bits> <code>``
        Vector binary value change; two tokens.
    ``r<real> <code>``  /  ``R<real> <code>``
        Real (floating-point) value change; two tokens.
    ``$dumpvars`` / ``$dumpall`` / ``$dumpon`` / ``$dumpoff``
        Transparent grouping blocks; changes inside are processed normally.
    ``$comment … $end``
        Skipped.
    ``$end``
        Closes a dump block (or is stray); ignored.
    Unknown ``$keyword … $end``
        Warning issued, block skipped.
    """
    current_time: int = 0

    for token in tokens:
        first = token[0]

        # ── Timestamp ────────────────────────────────────────────────────────
        if first == "#":
            try:
                current_time = int(token[1:])
            except ValueError:
                raise VCDParseError(f"Invalid timestamp token: {token!r}")

        # ── Scalar value change ───────────────────────────────────────────────
        elif first in "01xzXZ" and len(token) > 1:
            _apply_change(model, token[1:], first.lower(), current_time)

        # ── Vector (binary) value change ─────────────────────────────────────
        elif first in "bB":
            bits = token[1:].lower()
            code = _expect(tokens, "vector identifier code after b-value")
            _apply_change(model, code, bits, current_time)

        # ── Real value change ─────────────────────────────────────────────────
        elif first in "rR":
            real_str = token[1:]
            code = _expect(tokens, "real identifier code after r-value")
            _apply_change(model, code, real_str, current_time)

        # ── Dump-block keywords (transparent) ────────────────────────────────
        elif token in ("$dumpvars", "$dumpall", "$dumpon", "$dumpoff"):
            # The changes inside are regular value-change tokens; they will be
            # processed by the loop above.  The closing $end is caught below.
            pass

        # ── Comment ───────────────────────────────────────────────────────────
        elif token == "$comment":
            _skip_to_end(tokens)

        # ── Block closer (dump block end, or stray) ───────────────────────────
        elif token == "$end":
            pass

        # ── Unknown keyword ───────────────────────────────────────────────────
        elif first == "$":
            warnings.warn(
                f"Unknown simulation keyword {token!r}; skipping block",
                VCDWarning,
                stacklevel=3,
            )
            _skip_to_end(tokens)

        # ── Lone value character (malformed VCD) ──────────────────────────────
        elif first in "01xzXZ":
            warnings.warn(
                f"Standalone value token {token!r} at time {current_time} "
                "has no identifier code; ignoring",
                VCDWarning,
                stacklevel=3,
            )

        # ── Anything else ─────────────────────────────────────────────────────
        else:
            warnings.warn(
                f"Unrecognised token {token!r} at time {current_time}; ignoring",
                VCDWarning,
                stacklevel=3,
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(source: "str | Path | TextIO") -> VCDModel:
    """Parse a VCD file and return a :class:`~vcd2wavedrom.model.VCDModel`.

    Parameters
    ----------
    source
        Path to a VCD file (``str`` or :class:`pathlib.Path`), or an
        already-opened text stream (anything with a ``__iter__`` that yields
        lines, including ``sys.stdin``).

    Returns
    -------
    VCDModel
        Fully populated model with all ``Signal.changes`` lists filled in.

    Raises
    ------
    VCDParseError
        On malformed input: bad timestamps, ill-formed ``$var``, unexpected
        EOF inside a declaration, etc.

    Warns
    -----
    VCDWarning
        On non-fatal issues: unknown keywords (skipped), undeclared identifier
        codes, out-of-range code characters.
    """
    if isinstance(source, (str, Path)):
        with open(source, "r") as fh:
            return _parse_stream(fh)
    return _parse_stream(source)


def _parse_stream(stream: TextIO) -> VCDModel:
    tokens = tokenise(stream)
    model = _parse_header(tokens)
    _parse_body(tokens, model)
    return model
