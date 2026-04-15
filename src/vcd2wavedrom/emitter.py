"""Timeline discretisation and WaveJSON emission.

Three public functions form the pipeline:

    build_timeline  → list[int]            tick timestamps
    build_wave      → (wave_str, data)     per-signal encoding
    emit            → dict                 final WaveJSON dict
"""

from __future__ import annotations

import warnings
from typing import Any

from .model import Signal, VCDModel, VCDWarning


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

def build_timeline(model: VCDModel, period: int | None = None) -> list[int]:
    """Return the ordered list of tick timestamps.

    Parameters
    ----------
    model:
        Populated VCDModel.
    period:
        ``None`` (default) — **event-driven**: one tick per distinct change
        timestamp.  Every transition is visible regardless of timing
        irregularity.

        ``N`` — **clock-period sampling**: ticks at ``0, N, 2N, …`` up to the
        last change timestamp.  Transitions between sample points appear at
        the next sample.
    """
    all_times: set[int] = set()
    for sig in model.signals.values():
        for t, _ in sig.changes:
            all_times.add(t)

    if not all_times:
        warnings.warn(
            "VCD has no simulation events; WaveJSON output will be empty",
            VCDWarning,
            stacklevel=3,
        )
        return []

    if period is None:
        return sorted(all_times)

    # Clock-period sampling
    if period <= 0:
        raise ValueError(f"period must be a positive integer, got {period!r}")
    max_time = max(all_times)
    result: list[int] = []
    t = 0
    while t <= max_time:
        result.append(t)
        t += period
    return result


# ---------------------------------------------------------------------------
# Value encoding helpers
# ---------------------------------------------------------------------------

def _expand_bits(bits: str, width: int) -> str:
    """Pad *bits* to exactly *width* characters.

    Extension rule (IEEE 1800-2023 §21.7.2.1):
    - If the leftmost character is ``x`` or ``z``, extend with that character.
    - Otherwise (``0`` or ``1``), extend with ``0`` (zero-extension).
    """
    if len(bits) >= width:
        return bits[-width:]          # guard: truncate if oversized
    pad = bits[0] if bits[0] in "xz" else "0"
    return pad * (width - len(bits)) + bits


def _encode_value(value: str, width: int, kind: str) -> tuple[str, str | None]:
    """Translate a raw VCD value string into a WaveJSON character.

    Returns
    -------
    (wave_char, data_entry)
        *data_entry* is ``None`` for binary states.  It carries the printable
        value string only when *wave_char* is ``"="`` (multi-bit numeric or
        real-typed signal).

    Encoding rules (DESIGN.md §4.4)
    --------------------------------
    - ``real``-typed signals → ``"="`` + decimal string data.
    - width=1 scalars → character is the value directly (``0/1/x/z``).
    - width>1 vectors:
      - any ``x`` bit → ``"x"``
      - any ``z`` bit (no ``x``) → ``"z"``
      - all ``0`` → ``"0"``
      - all ``1`` → ``"1"``
      - otherwise → ``"="`` + ``"0x{hex}"`` data entry
    """
    # Real-typed variable → data lane
    if kind == "real":
        return "=", value

    # Width-1 scalar
    if width == 1:
        return (value if value in "01xz" else "x"), None

    # Multi-bit vector
    expanded = _expand_bits(value, width)
    if "x" in expanded:
        return "x", None
    if "z" in expanded:
        return "z", None
    if all(b == "0" for b in expanded):
        return "0", None
    if all(b == "1" for b in expanded):
        return "1", None
    return "=", f"0x{int(expanded, 2):x}"


# ---------------------------------------------------------------------------
# Wave string builder
# ---------------------------------------------------------------------------

def build_wave(signal: Signal, timeline: list[int]) -> tuple[str, list[str]]:
    """Build the WaveJSON wave string and data list for *signal*.

    Algorithm (DESIGN.md §4.4)
    --------------------------
    1. Walk *timeline* left to right.
    2. If the signal has a change **at** the current tick timestamp → emit the
       encoded character (``0/1/x/z/=``).
    3. If there is **no** change at this tick → emit ``"."`` (hold).
    4. The very first tick always emits an explicit character, never ``"."``.
       If the signal has no value at the first tick, ``"x"`` (unknown) is used.

    For ``"="`` characters each corresponds to one entry in *data_list*,
    populated in tick order.

    Parameters
    ----------
    signal:
        Signal whose ``changes`` list is to be rendered.
    timeline:
        Ordered list of tick timestamps from :func:`build_timeline`.

    Returns
    -------
    (wave_string, data_list)
    """
    if not timeline:
        return "", []

    # Build time → value lookup.  If the same timestamp appears multiple times
    # in changes (rare but possible), the last entry wins.
    change_map: dict[int, str] = {}
    for t, v in signal.changes:
        change_map[t] = v

    wave_chars: list[str] = []
    data: list[str] = []
    current_char: str | None = None   # last emitted non-dot character

    for t in timeline:
        if t in change_map:
            char, data_entry = _encode_value(
                change_map[t], signal.width, signal.kind
            )
            wave_chars.append(char)
            current_char = char
            if data_entry is not None:
                data.append(data_entry)
        else:
            if current_char is None:
                # No value seen yet at the very first tick → unknown
                wave_chars.append("x")
                current_char = "x"
            else:
                wave_chars.append(".")

    return "".join(wave_chars), data


# ---------------------------------------------------------------------------
# Signal selection
# ---------------------------------------------------------------------------

def _select_signals(
    model: VCDModel,
    filter_names: list[str] | None,
) -> list[Signal]:
    """Return the signals to include in the output.

    A name matches a signal path if it equals the full path *or* is a
    dot-preceded suffix.  E.g. ``"clk"`` matches ``"tb.clk"`` but NOT
    ``"tb.sysclk"``.  Unmatched names emit a :class:`VCDWarning`.
    """
    if filter_names is None:
        return list(model.signals.values())

    selected: list[Signal] = []
    for name in filter_names:
        name = name.strip()
        matched = False
        for path, sig in model.signals.items():
            if path == name or path.endswith("." + name):
                selected.append(sig)
                matched = True
        if not matched:
            warnings.warn(
                f"--signals filter {name!r} matched no signals; skipping",
                VCDWarning,
                stacklevel=3,
            )
    return selected


# ---------------------------------------------------------------------------
# Hierarchy grouping
# ---------------------------------------------------------------------------

class _ScopeNode:
    """Tree node used to organise signals by scope for grouped output."""

    __slots__ = ("signals", "children")

    def __init__(self) -> None:
        self.signals: list[dict] = []
        self.children: dict[str, "_ScopeNode"] = {}


def _build_scope_tree(selected: list[Signal], timeline: list[int]) -> _ScopeNode:
    """Place each selected signal into a scope tree and build its wave."""
    root = _ScopeNode()
    for sig in selected:
        parts = sig.path.split(".")
        node = root
        for part in parts[:-1]:
            if part not in node.children:
                node.children[part] = _ScopeNode()
            node = node.children[part]
        wave, data = build_wave(sig, timeline)
        entry: dict[str, Any] = {"name": parts[-1], "wave": wave}
        if data:
            entry["data"] = data
        node.signals.append(entry)
    return root


def _node_to_wavejson_list(node: _ScopeNode, label: str | None = None) -> list:
    """Recursively convert a _ScopeNode into a WaveJSON group list.

    The first element of the list is the group *label* (scope name), followed
    by signal dicts and nested group lists — exactly the WaveJSON group format.
    """
    result: list = []
    if label is not None:
        result.append(label)
    result.extend(node.signals)
    for child_name, child_node in node.children.items():
        result.append(_node_to_wavejson_list(child_node, child_name))
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def emit(
    model: VCDModel,
    signals: "list[str] | None" = None,
    period: "int | None" = None,
    group: bool = False,
) -> dict:
    """Build and return a WaveJSON-compatible dict.

    Parameters
    ----------
    model:
        Fully populated VCDModel from the parser.
    signals:
        If given, only signals whose path equals or ends with one of these
        names are included.  Each name is matched as a full path or
        dot-preceded suffix.
    period:
        Tick sampling period in VCD time units.  ``None`` = event-driven
        (one tick per distinct change timestamp).
    group:
        If ``True``, wrap signals in WaveJSON group arrays that mirror the
        VCD scope hierarchy.

    Returns
    -------
    dict
        ``{"head": {"tock": 1}, "signal": [...]}`` ready for ``json.dump``.
    """
    timeline = build_timeline(model, period)
    selected = _select_signals(model, signals)

    if group:
        root = _build_scope_tree(selected, timeline)
        signal_list: list = []
        # Top-level signals (those with no scope prefix) come first
        signal_list.extend(root.signals)
        # Then each top-level scope group as a nested list
        for scope_name, child in root.children.items():
            signal_list.append(_node_to_wavejson_list(child, scope_name))
    else:
        signal_list = []
        for sig in selected:
            wave, data = build_wave(sig, timeline)
            entry: dict[str, Any] = {"name": sig.path, "wave": wave}
            if data:
                entry["data"] = data
            signal_list.append(entry)

    return {"head": {"tock": 1}, "signal": signal_list}
