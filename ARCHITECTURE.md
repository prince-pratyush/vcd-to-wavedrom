# Architecture — vcd2wavedrom

## 1. Repository Layout

```
vcd-to-wavedrom/
├── src/
│   └── vcd2wavedrom/
│       ├── __init__.py        # package version export
│       ├── cli.py             # entry point, argparse, orchestration
│       ├── parser.py          # VCD tokeniser + two-phase parser
│       ├── model.py           # Signal, VCDModel dataclasses
│       └── emitter.py         # timeline discretisation + WaveJSON emission
├── tests/
│   ├── __init__.py
│   ├── fixtures/
│   │   ├── simple.vcd         # minimal hand-crafted VCD
│   │   ├── simple_expected.json
│   │   ├── example.vcd        # VCD from task brief
│   │   └── example_expected.json
│   ├── test_parser.py         # unit tests for parser.py
│   ├── test_emitter.py        # unit tests for emitter.py
│   └── test_cli.py            # integration tests via subprocess
├── samples/
│   ├── counter.vcd            # sample input shipped with the repo
│   └── counter.json           # corresponding WaveJSON output
├── pyproject.toml
├── README.md
├── DESIGN.md
└── ARCHITECTURE.md
```

---

## 2. Module Responsibilities

### 2.1 `model.py` — Data Structures

The single source of truth for internal representations.
No parsing logic. No output logic. Pure dataclasses.

```
┌─────────────────────────────────────┐
│  Signal                             │
│  ─────────────────────────────────  │
│  path:    str                       │
│  code:    str                       │
│  width:   int                       │
│  kind:    str                       │
│  changes: list[tuple[int, str]]     │
└─────────────────────────────────────┘

┌─────────────────────────────────────┐
│  VCDModel                           │
│  ─────────────────────────────────  │
│  timescale: str                     │
│  date:      str                     │
│  version:   str                     │
│  signals:   dict[str, Signal]       │  path → Signal
│  codes:     dict[str, list[Signal]] │  code → [Signal, ...]
└─────────────────────────────────────┘
```

**Why separate model from parser:** The emitter, tests, and any future alternate
parsers (e.g. a streaming FSDB adapter) all work against `VCDModel` directly.
Keeping data structures in their own module prevents circular imports and makes
the model independently testable.

---

### 2.2 `parser.py` — VCD Parser

Responsibility: read a VCD file (or stdin) and return a `VCDModel`.

#### Tokeniser

```python
def tokenise(stream: TextIO) -> Iterator[str]:
    """Yield whitespace-delimited tokens from a VCD stream."""
```

VCD is not line-oriented — keywords and values can span lines freely.
The tokeniser yields one token at a time without buffering the file.

#### Phase 1 — Header parser

```python
def _parse_header(tokens: Iterator[str]) -> VCDModel:
    """
    Consume tokens up to and including $enddefinitions $end.
    Returns a VCDModel with signals and codes populated, changes empty.
    """
```

State machine with states:
```
IDLE → IN_KEYWORD → (DATE | VERSION | TIMESCALE | SCOPE | VAR | COMMENT | ENDDEFS)
```

Scope tracking uses a stack of scope names. At each `$var`, the full path is
assembled by joining the stack with `.`, then the Signal is registered in both
`model.signals` and `model.codes`.

#### Phase 2 — Body parser

```python
def _parse_body(tokens: Iterator[str], model: VCDModel) -> None:
    """
    Consume the simulation body, appending (time, value) to each Signal.changes.
    Mutates model in place.
    """
```

Current timestamp is tracked as `current_time: int`. On each `#<N>` token,
`current_time` is updated. Scalar changes (`0!`) are split on the first character.
Vector changes (`b<bits> <code>`) consume two tokens.

`$dumpvars`, `$dumpall`, `$dumpon`, `$dumpoff` are treated as transparent
grouping blocks — the changes inside are processed normally, and `$end` closes
the block.

#### Public API

```python
def parse(source: str | Path | TextIO) -> VCDModel:
    """Parse a VCD file and return a VCDModel. Raises VCDParseError on failure."""
```

---

### 2.3 `emitter.py` — WaveJSON Emitter

Responsibility: take a `VCDModel` and emit a WaveJSON-compatible dict.

#### Timeline builder

```python
def build_timeline(model: VCDModel, period: int | None = None) -> list[int]:
    """
    Return the ordered list of tick timestamps.
    period=None → event-driven (one tick per distinct change timestamp).
    period=N    → clock-period sampling at multiples of N.
    """
```

#### Wave string builder

```python
def build_wave(signal: Signal, timeline: list[int]) -> tuple[str, list[str]]:
    """
    Returns (wave_string, data_list).
    data_list is non-empty only for multi-bit signals using '=' encoding.
    """
```

Logic:
1. Build a `{time: value}` lookup from `signal.changes`.
2. Walk `timeline`. At each tick, lookup value or hold previous.
3. Emit character per tick; collapse runs of unchanged binary value to `.`.
4. For vectors: if all-0 → `0`, all-1 → `1`, any-x → `x`, any-z → `z`,
   otherwise `=` + append hex to data list.

#### Emitter

```python
def emit(
    model: VCDModel,
    signals: list[str] | None = None,
    period: int | None = None,
    group: bool = False,
) -> dict:
    """
    Build and return a WaveJSON dict.
    signals: if provided, filter by suffix or full path match.
    period:  tick sampling period (None = event-driven).
    group:   wrap signals in WaveJSON group arrays by scope.
    """
```

---

### 2.4 `cli.py` — Entry Point

```
vcd2wavedrom [OPTIONS] [FILE]

Arguments:
  FILE                  Path to VCD file. Reads stdin if omitted.

Options:
  --signals, -s TEXT    Comma-separated signal names to include.
  --period, -p INT      Sampling period in VCD time units.
  --group, -g           Group signals by scope hierarchy.
  --output, -o PATH     Output file path. Defaults to stdout.
  --pretty              Pretty-print JSON output.
  --version             Show version and exit.
  --help                Show this message and exit.
```

Orchestration:

```
cli.main()
  │
  ├─ parse args
  ├─ open source (file or stdin)
  ├─ parser.parse(source)         → VCDModel
  ├─ emitter.emit(model, ...)     → dict
  └─ json.dump(result, output)
```

Errors from `VCDParseError` are caught at the CLI layer and printed to stderr
with a non-zero exit code. All warnings from parser and emitter go to stderr
so stdout remains clean JSON.

---

## 3. Data Flow

```
 ┌──────────┐     tokenise()      ┌──────────────┐
 │  VCD     │ ──────────────────► │  token       │
 │  file    │                     │  stream      │
 └──────────┘                     └──────┬───────┘
                                         │
                              _parse_header()
                                         │
                                         ▼
                                  ┌─────────────┐
                                  │  VCDModel   │
                                  │  (no        │
                                  │   changes)  │
                                  └──────┬──────┘
                                         │
                              _parse_body()
                                         │
                                         ▼
                                  ┌─────────────┐
                                  │  VCDModel   │
                                  │  (with      │
                                  │   changes)  │
                                  └──────┬──────┘
                                         │
                              build_timeline()
                                         │
                                         ▼
                                  ┌─────────────┐
                                  │  timeline   │
                                  │  [t0,t1...] │
                                  └──────┬──────┘
                                         │
                         ┌───────────────┘
                         │  for each selected signal:
                         │  build_wave(signal, timeline)
                         ▼
                  ┌─────────────┐
                  │  WaveJSON   │
                  │  dict       │
                  └──────┬──────┘
                         │
                  json.dump()
                         │
                         ▼
                    stdout / file
```

---

## 4. Error Hierarchy

```
VCDError (base)
├── VCDParseError       raised by parser.py on malformed input
└── VCDEmitError        raised by emitter.py on irrecoverable emit failure
```

Warnings (non-fatal) use `warnings.warn()` with a custom `VCDWarning` category
so callers can suppress or capture them programmatically.

---

## 5. Testing Strategy

### Unit tests — `test_parser.py`

| Test | What it covers |
|---|---|
| `test_parse_header_basic` | timescale, date, version extraction |
| `test_parse_single_scope` | one scope, multiple variables |
| `test_parse_nested_scopes` | path assembly with $scope/$upscope |
| `test_parse_alias_codes` | same code declared in two scopes |
| `test_parse_scalar_changes` | 0/1/x/z scalar value changes |
| `test_parse_vector_changes` | b-format vector changes |
| `test_parse_dumpvars_block` | initial value block |
| `test_parse_unknown_keyword` | tolerant skip with warning |
| `test_parse_malformed_raises` | VCDParseError on bad input |

### Unit tests — `test_emitter.py`

| Test | What it covers |
|---|---|
| `test_timeline_event_driven` | tick list from change timestamps |
| `test_timeline_period_mode` | clock-period sampling |
| `test_wave_scalar_hold` | `.` for unchanged ticks |
| `test_wave_scalar_xz` | x and z character emission |
| `test_wave_vector_binary` | all-0 / all-1 encoding |
| `test_wave_vector_data` | `=` encoding + data list |
| `test_emit_signal_filter` | --signals selection |
| `test_emit_grouping` | WaveJSON group arrays |
| `test_emit_example_fixture` | full round-trip against task brief example |

### Integration tests — `test_cli.py`

Run the CLI as a subprocess against fixture VCDs and diff the JSON output
against expected fixtures. Covers stdin mode and file mode.

---

## 6. Extension Points

| Extension | Where to add it |
|---|---|
| FSDB / other input formats | New `parser_fsdb.py` returning `VCDModel` |
| SVG direct export | New `emitter_svg.py` using WaveDrom JS via node |
| Config file for signal aliases | `cli.py` + new `config.py` |
| Streaming large VCDs | Replace `Signal.changes: list` with a generator protocol |
