# vcd2wavedrom

Convert [VCD (Value Change Dump)](https://en.wikipedia.org/wiki/Value_change_dump)
simulation files to [WaveDrom](https://wavedrom.com/) WaveJSON.

```
vcd2wavedrom simulation.vcd --pretty | pbcopy   # paste into wavedrom.com/editor
```

---

## Requirements

- Python 3.10 or later
- No runtime dependencies (standard library only)

---

## Installation

```bash
# Clone and install in editable mode
git clone <repo-url>
cd vcd-to-wavedrom
pip install -e .
```

After installation the `vcd2wavedrom` command is available on your PATH.

---

## Quick start

```bash
# Convert a VCD file, pretty-print the output
vcd2wavedrom samples/counter.vcd --pretty

# Read from stdin
cat simulation.vcd | vcd2wavedrom

# Write output to a file instead of stdout
vcd2wavedrom simulation.vcd -o diagram.json

# Show only specific signals
vcd2wavedrom simulation.vcd -s clk,data,count

# Group signals by module hierarchy
vcd2wavedrom simulation.vcd --group --pretty

# Clock-period sampling (one tick per 10 ns instead of one per event)
vcd2wavedrom simulation.vcd --period 10
```

---

## CLI Reference

```
vcd2wavedrom [OPTIONS] [FILE]
```

| Argument / Option | Description |
|---|---|
| `FILE` | Path to VCD file. Reads **stdin** if omitted. |
| `-s / --signals NAMES` | Comma-separated signal names to include. Each name is matched as a full hierarchical path or a dot-preceded suffix. Example: `-s clk,tb.data` |
| `-p / --period N` | Sample signal state every N VCD time units instead of using event-driven ticks. |
| `-g / --group` | Wrap signals in WaveJSON group arrays mirroring the VCD scope hierarchy. |
| `-o / --output PATH` | Write JSON to a file instead of stdout. |
| `--pretty` | Pretty-print JSON with 2-space indentation. |
| `--version` | Print version and exit. |
| `--help` | Print usage and exit. |

**Stdout** always contains clean JSON. **Stderr** carries warnings and errors.
This means the tool is safe to use in pipelines:

```bash
vcd2wavedrom sim.vcd | python -m json.tool
vcd2wavedrom sim.vcd | jq '.signal | map(.name)'
```

---

## Output format

The tool produces [WaveJSON](https://github.com/wavedrom/schema/blob/master/WaveJSON.md):

```json
{
  "head": { "tock": 1 },
  "signal": [
    { "name": "tb.clk",   "wave": "010101" },
    { "name": "tb.count", "wave": "0=.=.=.", "data": ["0x1", "0x2", "0x3"] }
  ]
}
```

Paste directly into the [WaveDrom editor](https://wavedrom.com/editor.html) to
render the timing diagram.

### Wave characters

| Character | Meaning |
|---|---|
| `0` | Logic low |
| `1` | Logic high |
| `x` | Unknown / don't-care |
| `z` | High impedance |
| `=` | Multi-bit data value (hex in `data` array) |
| `.` | Hold previous value (no change this tick) |

---

## Sample

The `samples/` directory contains a ready-to-use example:

```bash
vcd2wavedrom samples/counter.vcd --pretty
```

`samples/counter.vcd` — a 4-bit binary counter with a `done` flag that pulses
when the count reaches 7.

`samples/counter.json` — the corresponding WaveJSON output.

---

## Running the tests

```bash
# Install test dependencies
pip install pytest

# Run the full test suite (172 tests)
pytest

# Run only parser tests
pytest tests/test_parser.py -v

# Run only CLI integration tests
pytest tests/test_cli.py -v
```

---

## Standards compliance

The parser follows **IEEE Std 1800-2023 §21.7** (Value Change Dump files).
Where the 2023 standard is not accessible, **IEEE Std 1800-2017 §21.7**
(MIT release) is equivalent for VCD purposes.

Non-standard simulator extensions (e.g. Icarus Verilog `$ivl_*` scopes)
are parsed tolerantly: unknown `$keywords` emit a warning to stderr and
are skipped rather than causing the converter to abort.

See [`DESIGN.md`](DESIGN.md) for a full account of parsing strategy, timeline
discretisation, vector encoding, and known limitations.

---

## Project layout

```
src/vcd2wavedrom/
    __init__.py   — package version + public API
    model.py      — Signal, VCDModel dataclasses; error hierarchy
    parser.py     — VCD tokeniser + two-phase parser
    emitter.py    — timeline builder + WaveJSON emitter
    cli.py        — argparse entry point

tests/
    fixtures/     — simple.vcd, example.vcd and their expected JSON
    test_parser.py
    test_emitter.py
    test_cli.py

samples/
    counter.vcd   — 4-bit counter sample
    counter.json  — corresponding WaveJSON output
```
