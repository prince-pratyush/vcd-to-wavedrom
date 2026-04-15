"""Internal data model for vcd2wavedrom.

Pure dataclasses — no parsing or output logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class VCDError(Exception):
    """Base class for all vcd2wavedrom errors."""


class VCDParseError(VCDError):
    """Raised by the parser on malformed VCD input."""


class VCDEmitError(VCDError):
    """Raised by the emitter on an irrecoverable emission failure."""


class VCDWarning(UserWarning):
    """Non-fatal warnings from the parser or emitter.

    Callers can suppress or capture these with the standard ``warnings`` API.
    """


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """A single declared VCD variable and its value-change history.

    Attributes
    ----------
    path:
        Full hierarchical name, e.g. ``"tb_circuit.uut.and1_out"``.
    code:
        VCD identifier code — one or more printable ASCII characters
        (IEEE 1800-2023 §21.7.2.1, range ``!`` 0x21 through ``~`` 0x7e).
    width:
        Declared bit width.
    kind:
        Variable type string from the VCD declaration: ``"wire"``, ``"reg"``,
        ``"integer"``, ``"real"``, etc.
    changes:
        Ordered list of ``(absolute_time, value_string)`` pairs appended
        during Phase 2 parsing.

        ``value_string`` is:

        * ``"0"`` / ``"1"`` / ``"x"`` / ``"z"`` for width-1 signals, and
        * a binary string of characters ``{0, 1, x, z}`` for wider signals,
          without zero-padding to full width (the emitter pads on read).
    """

    path: str
    code: str
    width: int
    kind: str
    changes: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class VCDModel:
    """Top-level model produced by the VCD parser.

    Attributes
    ----------
    timescale:
        Raw timescale string from the ``$timescale`` block, e.g. ``"1ps"``.
    date:
        Content of the ``$date`` block (whitespace-stripped).
    version:
        Content of the ``$version`` block (whitespace-stripped).
    signals:
        Mapping of full hierarchical path → :class:`Signal`.
    codes:
        Mapping of VCD identifier code → list of :class:`Signal` objects that
        share that code.  A single code maps to multiple signals when the same
        identifier is re-declared under different scopes (aliasing per
        IEEE 1800-2023 §21.7.2.1).
    """

    timescale: str = ""
    date: str = ""
    version: str = ""
    signals: dict[str, Signal] = field(default_factory=dict)
    codes: dict[str, list[Signal]] = field(default_factory=dict)
