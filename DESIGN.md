# Design Note — vcd2wavedrom

## 1. Overview

`vcd2wavedrom` is a command-line converter that parses Value Change Dump (VCD) files
produced by hardware simulators and emits WaveJSON suitable for rendering with WaveDrom.

The converter is designed for correctness against IEEE 1800-2023, readable output,
and practical usability on real simulation dumps.

---

## 2. Parsing Strategy

### 2.1 Two-phase parse

The converter uses a two-phase approach:

**Phase 1 — Header and declarations**

Reads everything before `$enddefinitions $end`. This establishes:
- Simulation metadata (`$date`, `$version`, `$timescale`)
- Signal hierarchy via `$scope` / `$upscope` blocks
- Variable declarations (`$var`), mapping each identifier code to a signal

**Phase 2 — Simulation body**

Reads everything after `$enddefinitions $end`. This processes:
- Timestamp markers (`#<time>`)
- Scalar value changes (`0!`, `1"`, `x#`, `z$`)
- Vector value changes (`b<bits> <code>`, `r<real> <code>`)
- `$dumpvars`, `$dumpall`, `$dumpon`, `$dumpoff` blocks

The two phases are kept strictly separate. Phase 1 builds the signal registry;
Phase 2 streams value changes against it. This means the converter only needs to
hold the current signal state in memory, not the full change history, until output time.

### 2.2 Tokeniser

VCD is whitespace-delimited and keyword-driven. The tokeniser splits on whitespace
and yields tokens one at a time. Keywords start with `$`. The parser is a simple
state machine — no grammar library required.

### 2.3 Identifier codes

VCD assigns each declared variable a short "identifier code" (one or more printable
ASCII characters, `!` through `~`). The same code may be reused across scopes to
alias signals (IEEE 1800-2023 §21.7.2.1). The parser builds a code→signal map at
declaration time. When a code aliases multiple signals, all aliased signals are
updated on each value change.

### 2.4 Standards assumptions

- Follows IEEE 1800-2023 §21.7 (Value Change Dump files) as the authoritative reference.
- `$dumpoff` value changes (forced to `x`) are represented faithfully.
- `real` typed variables are included in the internal model but emitted as `=`-style
  data lanes in WaveJSON rather than binary wave strings.
- Non-standard simulator extensions (e.g. Icarus `$ivl_*` scopes) are parsed
  tolerantly — unknown `$keywords` are skipped with a warning rather than aborting.

---

## 3. Internal Data Model

### 3.1 Signal

Each declared variable is represented as a `Signal`:

```
Signal
  path: str           # full hierarchical name, e.g. "tb_circuit.uut.and1_out"
  code: str           # VCD identifier code
  width: int          # bit width
  kind: str           # "wire" | "reg" | "integer" | "real" | ...
  changes: list[(time: int, value: str)]
                      # ordered list of (absolute_time, value_string)
                      # value_string: "0","1","x","z" for scalars
                      #               "01xz..." binary string for vectors
```

### 3.2 VCDModel

The top-level model produced by the parser:

```
VCDModel
  timescale: str      # e.g. "1ps"
  date: str
  version: str
  signals: dict[str, Signal]   # path → Signal
  codes: dict[str, list[Signal]]  # code → [Signal, ...]  (handles aliasing)
```

### 3.3 Why store raw changes, not pre-rendered waves

The emitter needs to know the full change history to discretise onto an arbitrary
tick grid. Storing raw `(time, value)` pairs keeps the parser simple and the emitter
flexible — the tick resolution can be chosen at emit time without re-parsing.

---

## 4. Timeline Discretisation

This is the most consequential design decision in the converter.

### 4.1 The problem

WaveDrom wave strings are tick-relative: each character represents one clock period.
VCD timestamps are absolute integers (in units of the declared timescale). The two
must be reconciled.

### 4.2 Strategy — event-driven ticks

Rather than sampling at a fixed clock period (which would require the user to supply
a clock frequency or risk missing transitions), the converter defaults to
**event-driven ticks**: each unique timestamp in the VCD that carries at least one
value change becomes one tick.

This means:
- No transitions are lost regardless of timing irregularity.
- The output tick count equals the number of distinct change timestamps.
- The `"head": {"tock": 1}` field is set by default; users can override tick labels.

### 4.3 Clock-period mode (optional flag)

With `--period <N>` the converter samples signal state at multiples of N (in VCD
time units). Transitions that fall between sample points are visible at the next
sample. This is appropriate when the VCD was generated from a clocked design and
the user wants a clean clock-aligned diagram.

### 4.4 Wave string construction

Given a signal's change list and a tick timeline `[t0, t1, t2, ...]`:

1. Walk ticks left to right.
2. At each tick, check if the signal has a change at that timestamp.
   - If yes: emit the appropriate character (`0`, `1`, `x`, `z`, `=`).
   - If no: emit `.` (hold previous value).
3. The first tick always emits an explicit value, never `.`.

For multi-bit signals (width > 1), value characters map as:
- All-zero → `0`
- All-one → `1`
- Any x bit → `x`
- Any z bit → `z`
- Mixed numeric → `=` with `data` array entry containing the hex value

---

## 5. WaveJSON Emission

### 5.1 Output structure

```json
{
  "head": { "tock": 1 },
  "signal": [
    { "name": "tb_circuit.clk", "wave": "010101" },
    { "name": "tb_circuit.uut.out", "wave": "0..1.." },
    ["uut",
      { "name": "and1_out", "wave": "0.1..." }
    ]
  ]
}
```

### 5.2 Hierarchy grouping

By default the emitter produces a flat signal list using full dotted paths.
With `--group` the emitter wraps signals sharing a common scope prefix into
WaveJSON group arrays (the `["GroupName", {...}, {...}]` form). This mirrors
the VCD scope structure.

### 5.3 Signal selection

With `--signals a,b,c` only the named signals (matched by suffix or full path)
are included. If the VCD is large this is the primary way to keep the diagram legible.
Without `--signals`, all signals are emitted.

### 5.4 Data lanes for vectors

Wide vectors that carry changing data (e.g. a 32-bit counter) are emitted as:

```json
{ "name": "i[31:0]", "wave": "=.=.=.", "data": ["0x0", "0x1", "0x2"] }
```

The `data` array entries are populated in tick order for each `=` character.

---

## 6. Error Handling

| Condition | Behaviour |
|---|---|
| Malformed VCD keyword | Parse error with line number, exit non-zero |
| Unknown `$keyword` | Warning to stderr, token skipped |
| Value change for undeclared code | Warning to stderr, change ignored |
| Signal not found (--signals filter) | Warning to stderr, filter entry skipped |
| Empty VCD body | Valid empty JSON output, warning issued |

The converter never silently drops transitions for declared signals.

---

## 7. Complexity

| Operation | Complexity |
|---|---|
| Tokenisation | O(N) in file size |
| Declaration parse | O(V) in variable count |
| Body parse | O(C) in total change count |
| Wave string construction | O(T × V) in ticks × variables |
| JSON serialisation | O(T × V) |

For typical simulation dumps (thousands of signals, millions of timesteps) the
bottleneck is body parsing. The streaming Phase 2 parser avoids loading the full
change log into memory — changes are appended to per-signal lists and the input
is not buffered beyond one token at a time.

---

## 8. Known Limitations and Non-Standard Extensions

- `$dumpoff` / `$dumpon` blocks are parsed but the forced-x period is not
  specially annotated in the WaveJSON output.
- Icarus Verilog emits `$scope begin $ivl_for_loop0 $end` for generate loops —
  these are parsed as ordinary scopes with a non-standard name.
- `real`-typed variables are emitted as `=` lanes with decimal string data values;
  floating-point waveform rendering in WaveDrom is limited.
- VCD files with more than ~500 signals benefit strongly from `--signals` filtering;
  WaveDrom itself becomes slow above ~100 lanes.
- The converter does not validate that identifier codes stay within the printable
  ASCII range `!` (0x21) through `~` (0x7e) as required by IEEE 1800-2023 §21.7.2.1;
  out-of-range codes produce a warning.
