"""Unit tests for vcd2wavedrom.emitter.

Tests build VCDModel/Signal objects directly — no parser dependency —
so emitter behaviour is verified in complete isolation.
The final class does a full round-trip against the fixture files.
"""

import json
import warnings
from pathlib import Path

import pytest

from vcd2wavedrom.model import Signal, VCDModel, VCDWarning
from vcd2wavedrom.emitter import (
    build_timeline,
    build_wave,
    emit,
    _expand_bits,
    _encode_value,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signal(path="tb.sig", code="!", width=1, kind="wire", changes=None):
    """Build a Signal with optional change list."""
    sig = Signal(path=path, code=code, width=width, kind=kind)
    if changes:
        sig.changes = changes
    return sig


def _model(*signals):
    """Build a minimal VCDModel from a list of signals."""
    m = VCDModel()
    for sig in signals:
        m.signals[sig.path] = sig
        m.codes.setdefault(sig.code, []).append(sig)
    return m


# ---------------------------------------------------------------------------
# _expand_bits
# ---------------------------------------------------------------------------

class TestExpandBits:
    def test_zero_extend(self):
        assert _expand_bits("1", 4) == "0001"

    def test_already_full_width(self):
        assert _expand_bits("0101", 4) == "0101"

    def test_x_extend(self):
        assert _expand_bits("x", 4) == "xxxx"

    def test_z_extend(self):
        assert _expand_bits("z", 4) == "zzzz"

    def test_zero_string_extend(self):
        assert _expand_bits("0", 4) == "0000"

    def test_oversized_truncates(self):
        # Guard: if bits is somehow longer than width, take rightmost chars
        assert _expand_bits("10101", 4) == "0101"

    def test_mixed_1_prefix(self):
        # '1' prefix → zero-extend
        assert _expand_bits("1xz", 4) == "01xz"


# ---------------------------------------------------------------------------
# _encode_value
# ---------------------------------------------------------------------------

class TestEncodeValue:
    def test_scalar_0(self):
        assert _encode_value("0", 1, "wire") == ("0", None)

    def test_scalar_1(self):
        assert _encode_value("1", 1, "wire") == ("1", None)

    def test_scalar_x(self):
        assert _encode_value("x", 1, "wire") == ("x", None)

    def test_scalar_z(self):
        assert _encode_value("z", 1, "wire") == ("z", None)

    def test_vector_all_zero(self):
        assert _encode_value("0000", 4, "reg") == ("0", None)

    def test_vector_all_one(self):
        assert _encode_value("1111", 4, "reg") == ("1", None)

    def test_vector_any_x(self):
        char, data = _encode_value("01x0", 4, "reg")
        assert char == "x"
        assert data is None

    def test_vector_any_z(self):
        char, data = _encode_value("01z0", 4, "reg")
        assert char == "z"
        assert data is None

    def test_vector_x_beats_z(self):
        char, _ = _encode_value("xz", 2, "reg")
        assert char == "x"

    def test_vector_mixed_numeric(self):
        char, data = _encode_value("0001", 4, "reg")
        assert char == "="
        assert data == "0x1"

    def test_vector_hex_format(self):
        _, data = _encode_value("1010", 4, "reg")
        assert data == "0xa"

    def test_vector_short_string_zero_extended(self):
        # "1" for width=4 → "0001" → numeric → "0x1"
        char, data = _encode_value("1", 4, "reg")
        assert char == "="
        assert data == "0x1"

    def test_real_kind(self):
        char, data = _encode_value("3.14", 64, "real")
        assert char == "="
        assert data == "3.14"

    def test_real_integer_string(self):
        char, data = _encode_value("42", 32, "real")
        assert char == "="
        assert data == "42"


# ---------------------------------------------------------------------------
# build_timeline — event-driven
# ---------------------------------------------------------------------------

class TestTimelineEventDriven:
    def test_collects_all_unique_timestamps(self):
        sig = _signal(changes=[(0, "0"), (10, "1"), (20, "0")])
        assert build_timeline(_model(sig)) == [0, 10, 20]

    def test_merges_timestamps_across_signals(self):
        s1 = _signal("tb.a", "!", changes=[(0, "0"), (10, "1")])
        s2 = _signal("tb.b", '"', changes=[(0, "0"), (5, "1"), (10, "0")])
        assert build_timeline(_model(s1, s2)) == [0, 5, 10]

    def test_result_is_sorted(self):
        sig = _signal(changes=[(30, "0"), (10, "1"), (20, "0")])
        assert build_timeline(_model(sig)) == [10, 20, 30]

    def test_duplicate_timestamps_deduplicated(self):
        # Two signals both change at t=5 — should appear once
        s1 = _signal("tb.a", "!", changes=[(0, "0"), (5, "1")])
        s2 = _signal("tb.b", '"', changes=[(5, "0")])
        tl = build_timeline(_model(s1, s2))
        assert tl.count(5) == 1

    def test_empty_model_warns_and_returns_empty(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", VCDWarning)
            tl = build_timeline(VCDModel())
        assert tl == []
        assert any("empty" in str(w.message).lower() for w in caught)


# ---------------------------------------------------------------------------
# build_timeline — clock-period sampling
# ---------------------------------------------------------------------------

class TestTimelinePeriodMode:
    def test_samples_at_multiples(self):
        sig = _signal(changes=[(0, "0"), (25, "1")])
        assert build_timeline(_model(sig), period=10) == [0, 10, 20]

    def test_includes_zero(self):
        sig = _signal(changes=[(5, "1")])
        tl = build_timeline(_model(sig), period=5)
        assert tl[0] == 0

    def test_stops_at_max_change_time(self):
        sig = _signal(changes=[(0, "0"), (20, "1")])
        tl = build_timeline(_model(sig), period=10)
        assert max(tl) <= 20

    def test_period_one_equals_event_driven_if_all_consecutive(self):
        sig = _signal(changes=[(0, "0"), (1, "1"), (2, "0")])
        assert build_timeline(_model(sig), period=1) == [0, 1, 2]

    def test_invalid_period_raises(self):
        sig = _signal(changes=[(0, "0")])
        with pytest.raises(ValueError):
            build_timeline(_model(sig), period=0)

    def test_negative_period_raises(self):
        sig = _signal(changes=[(0, "0")])
        with pytest.raises(ValueError):
            build_timeline(_model(sig), period=-1)


# ---------------------------------------------------------------------------
# build_wave — scalar hold behaviour
# ---------------------------------------------------------------------------

class TestWaveScalarHold:
    def test_dot_for_unchanged_ticks(self):
        # Changes only at t=0 and t=20; ticks at 0,10,20
        sig = _signal(changes=[(0, "0"), (20, "1")])
        wave, _ = build_wave(sig, [0, 10, 20])
        assert wave == "0.1"

    def test_no_leading_dot(self):
        # First tick has no change — must NOT be '.'
        sig = _signal(changes=[(10, "1")])
        wave, _ = build_wave(sig, [0, 10, 20])
        assert wave[0] != "."

    def test_first_tick_x_when_no_initial_value(self):
        sig = _signal(changes=[(10, "1")])
        wave, _ = build_wave(sig, [0, 10, 20])
        assert wave[0] == "x"

    def test_hold_over_many_ticks(self):
        sig = _signal(changes=[(0, "1")])
        wave, _ = build_wave(sig, [0, 1, 2, 3, 4])
        assert wave == "1...."

    def test_empty_timeline(self):
        sig = _signal(changes=[(0, "0")])
        wave, data = build_wave(sig, [])
        assert wave == ""
        assert data == []

    def test_redundant_change_emits_character(self):
        """A change that sets the same value is still an explicit change."""
        sig = _signal(changes=[(0, "1"), (10, "1")])
        wave, _ = build_wave(sig, [0, 10, 20])
        # t=10 has a change → emit '1', not '.'
        assert wave[1] == "1"
        assert wave == "11."


# ---------------------------------------------------------------------------
# build_wave — x and z characters
# ---------------------------------------------------------------------------

class TestWaveScalarXZ:
    def test_x_emitted(self):
        sig = _signal(changes=[(0, "0"), (10, "x"), (20, "1")])
        wave, _ = build_wave(sig, [0, 10, 20])
        assert wave == "0x1"

    def test_z_emitted(self):
        sig = _signal(changes=[(0, "1"), (5, "z")])
        wave, _ = build_wave(sig, [0, 5])
        assert wave == "1z"

    def test_x_then_hold(self):
        sig = _signal(changes=[(0, "x")])
        wave, _ = build_wave(sig, [0, 1, 2])
        assert wave == "x.."


# ---------------------------------------------------------------------------
# build_wave — vector all-0 / all-1 encoding
# ---------------------------------------------------------------------------

class TestWaveVectorBinary:
    def test_all_zero_vector(self):
        sig = _signal(width=4, kind="reg", changes=[(0, "0000")])
        wave, data = build_wave(sig, [0])
        assert wave == "0"
        assert data == []

    def test_all_one_vector(self):
        sig = _signal(width=4, kind="reg", changes=[(0, "1111")])
        wave, data = build_wave(sig, [0])
        assert wave == "1"
        assert data == []

    def test_short_zero_string(self):
        # "0" for a 4-bit signal → expanded to "0000" → '0'
        sig = _signal(width=4, kind="reg", changes=[(0, "0")])
        wave, data = build_wave(sig, [0])
        assert wave == "0"
        assert data == []

    def test_x_in_vector(self):
        sig = _signal(width=4, kind="reg", changes=[(0, "01x0")])
        wave, _ = build_wave(sig, [0])
        assert wave == "x"

    def test_z_in_vector(self):
        sig = _signal(width=4, kind="reg", changes=[(0, "zzzz")])
        wave, _ = build_wave(sig, [0])
        assert wave == "z"


# ---------------------------------------------------------------------------
# build_wave — '=' encoding and data list
# ---------------------------------------------------------------------------

class TestWaveVectorData:
    def test_mixed_value_emits_equals(self):
        sig = _signal(width=4, kind="reg", changes=[(0, "0001")])
        wave, data = build_wave(sig, [0])
        assert wave == "="
        assert data == ["0x1"]

    def test_data_list_in_tick_order(self):
        sig = _signal(width=4, kind="reg",
                      changes=[(0, "0001"), (10, "0010"), (20, "0011")])
        wave, data = build_wave(sig, [0, 10, 20])
        assert wave == "==="
        assert data == ["0x1", "0x2", "0x3"]

    def test_data_only_for_equals(self):
        # t=0 → '0' (no data); t=10 → '=' (data); t=20 → '.' (no data)
        sig = _signal(width=4, kind="reg",
                      changes=[(0, "0000"), (10, "0001")])
        wave, data = build_wave(sig, [0, 10, 20])
        assert wave == "0=."
        assert data == ["0x1"]

    def test_real_signal_data(self):
        sig = _signal(width=64, kind="real", changes=[(0, "1.5"), (10, "2.5")])
        wave, data = build_wave(sig, [0, 10])
        assert wave == "=="
        assert data == ["1.5", "2.5"]

    def test_counter_hex_values(self):
        changes = [(i * 10, bin(i)[2:]) for i in range(5)]
        sig = _signal(width=8, kind="reg", changes=changes)
        wave, data = build_wave(sig, [i * 10 for i in range(5)])
        assert wave[0] == "0"      # i=0: all-zero
        assert wave[1] == "="      # i=1: 0x1
        assert data[0] == "0x1"


# ---------------------------------------------------------------------------
# emit — signal selection (--signals)
# ---------------------------------------------------------------------------

class TestEmitSignalFilter:
    def setup_method(self):
        s1 = _signal("tb.clk", "!", changes=[(0, "0"), (5, "1")])
        s2 = _signal("tb.data", '"', changes=[(0, "0"), (10, "1")])
        s3 = _signal("tb.sub.q", "#", changes=[(0, "0")])
        self.model = _model(s1, s2, s3)

    def test_no_filter_includes_all(self):
        result = emit(self.model)
        assert len(result["signal"]) == 3

    def test_suffix_match(self):
        result = emit(self.model, signals=["clk"])
        assert len(result["signal"]) == 1
        assert result["signal"][0]["name"] == "tb.clk"

    def test_full_path_match(self):
        result = emit(self.model, signals=["tb.data"])
        assert result["signal"][0]["name"] == "tb.data"

    def test_suffix_does_not_partial_match(self):
        # "lk" should NOT match "tb.clk" (must be dot-preceded suffix)
        result = emit(self.model, signals=["lk"])
        assert result["signal"] == []

    def test_multiple_signals(self):
        result = emit(self.model, signals=["clk", "data"])
        names = [s["name"] for s in result["signal"]]
        assert "tb.clk" in names
        assert "tb.data" in names
        assert "tb.sub.q" not in names

    def test_unmatched_filter_warns(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", VCDWarning)
            emit(self.model, signals=["nonexistent"])
        assert any("nonexistent" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# emit — WaveJSON group arrays (--group)
# ---------------------------------------------------------------------------

class TestEmitGrouping:
    def setup_method(self):
        s1 = _signal("tb.clk", "!", changes=[(0, "0")])
        s2 = _signal("tb.sub.q", '"', changes=[(0, "0")])
        s3 = _signal("tb.sub.d", "#", changes=[(0, "1")])
        self.model = _model(s1, s2, s3)

    def test_group_produces_nested_list(self):
        result = emit(self.model, group=True)
        # Expect a ["tb", ...] group in the signal list
        groups = [s for s in result["signal"] if isinstance(s, list)]
        assert len(groups) == 1
        assert groups[0][0] == "tb"

    def test_group_label_is_scope_name(self):
        result = emit(self.model, group=True)
        tb_group = result["signal"][0]
        assert tb_group[0] == "tb"

    def test_nested_scope_nested_list(self):
        result = emit(self.model, group=True)
        tb_group = result["signal"][0]
        # Inside "tb" group, "sub" scope should appear as a nested list
        sub_group = next(x for x in tb_group if isinstance(x, list))
        assert sub_group[0] == "sub"

    def test_signals_in_group_use_short_names(self):
        result = emit(self.model, group=True)
        tb_group = result["signal"][0]
        sub_group = next(x for x in tb_group if isinstance(x, list))
        sig_names = [s["name"] for s in sub_group if isinstance(s, dict)]
        assert "q" in sig_names
        assert "d" in sig_names

    def test_flat_uses_full_path_names(self):
        result = emit(self.model, group=False)
        names = [s["name"] for s in result["signal"]]
        assert "tb.clk" in names
        assert "tb.sub.q" in names


# ---------------------------------------------------------------------------
# emit — WaveJSON structure
# ---------------------------------------------------------------------------

class TestEmitStructure:
    def test_has_head_tock(self):
        result = emit(_model(_signal(changes=[(0, "0")])))
        assert result["head"] == {"tock": 1}

    def test_has_signal_list(self):
        result = emit(_model(_signal(changes=[(0, "0")])))
        assert "signal" in result
        assert isinstance(result["signal"], list)

    def test_signal_entry_has_name_and_wave(self):
        sig = _signal(changes=[(0, "0"), (5, "1")])
        result = emit(_model(sig))
        entry = result["signal"][0]
        assert "name" in entry
        assert "wave" in entry

    def test_data_key_present_only_for_vector(self):
        scalar = _signal("tb.s", "!", width=1, changes=[(0, "0")])
        vector = _signal("tb.v", '"', width=4, kind="reg",
                         changes=[(0, "0001")])
        result = emit(_model(scalar, vector))
        s_entry = next(e for e in result["signal"] if e["name"] == "tb.s")
        v_entry = next(e for e in result["signal"] if e["name"] == "tb.v")
        assert "data" not in s_entry
        assert "data" in v_entry

    def test_json_serialisable(self):
        sig = _signal(changes=[(0, "0"), (10, "1")])
        result = emit(_model(sig))
        # Must not raise
        json.dumps(result)


# ---------------------------------------------------------------------------
# Full round-trip — fixture files
# ---------------------------------------------------------------------------

class TestEmitExampleFixture:
    def test_simple_fixture_exact_match(self):
        """emit(simple.vcd) must produce the pre-recorded expected JSON."""
        from vcd2wavedrom.parser import parse as vcd_parse
        model = vcd_parse(FIXTURES / "simple.vcd")
        result = emit(model)
        expected = json.loads((FIXTURES / "simple_expected.json").read_text())
        assert result == expected

    def test_example_fixture_exact_match(self):
        """emit(example.vcd) must produce the pre-recorded expected JSON."""
        from vcd2wavedrom.parser import parse as vcd_parse
        model = vcd_parse(FIXTURES / "example.vcd")
        result = emit(model)
        expected = json.loads((FIXTURES / "example_expected.json").read_text())
        assert result == expected

    def test_example_signal_names_in_output(self):
        from vcd2wavedrom.parser import parse as vcd_parse
        model = vcd_parse(FIXTURES / "example.vcd")
        result = emit(model)
        names = [s["name"] for s in result["signal"]]
        assert "tb_circuit.out" in names
        assert "tb_circuit.w" in names
        assert "tb_circuit.$ivl_for_loop0.i" in names

    def test_example_counter_data_list(self):
        from vcd2wavedrom.parser import parse as vcd_parse
        model = vcd_parse(FIXTURES / "example.vcd")
        result = emit(model)
        counter = next(
            s for s in result["signal"]
            if s["name"] == "tb_circuit.$ivl_for_loop0.i"
        )
        assert counter["wave"][0] == "0"           # t=0: b0 = all-zero
        assert counter["data"][0] == "0x1"         # t=10000: b1
        assert counter["data"][-1] == "0x10"       # t=160000: b10000

    def test_example_period_sampling(self):
        """With --period 20000, ticks are 0..160000 in steps → 9 ticks."""
        from vcd2wavedrom.parser import parse as vcd_parse
        model = vcd_parse(FIXTURES / "example.vcd")
        result = emit(model, period=20000)
        w_entry = next(
            s for s in result["signal"] if s["name"] == "tb_circuit.w"
        )
        # 9 ticks: 0, 20000, 40000, 60000, 80000, 100000, 120000, 140000, 160000
        assert len(w_entry["wave"]) == 9
        # w changes at t=80000 (index 4 at period=20000)
        assert w_entry["wave"] == "0...1...."

    def test_example_signals_filter(self):
        from vcd2wavedrom.parser import parse as vcd_parse
        model = vcd_parse(FIXTURES / "example.vcd")
        result = emit(model, signals=["w", "x", "y", "z", "out"])
        # Matches both tb_circuit.X and tb_circuit.uut.X for each name
        assert len(result["signal"]) == 10  # 5 names × 2 aliases each
