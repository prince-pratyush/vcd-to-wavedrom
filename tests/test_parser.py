"""Unit tests for vcd2wavedrom.parser.

Each test is small and focused on one specific parser behaviour so that a
failure points directly to the broken functionality.
"""

import io
import warnings
from pathlib import Path

import pytest

from vcd2wavedrom.parser import parse, tokenise
from vcd2wavedrom.model import VCDParseError, VCDWarning

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(vcd: str):
    """Parse a VCD string and return a VCDModel."""
    return parse(io.StringIO(vcd))


def _parse_warn(vcd: str):
    """Parse a VCD string, return (model, list_of_warning_messages)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", VCDWarning)
        model = parse(io.StringIO(vcd))
    return model, [str(w.message) for w in caught]


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

class TestTokenise:
    def test_single_line(self):
        tokens = list(tokenise(io.StringIO("$timescale 1ns $end\n")))
        assert tokens == ["$timescale", "1ns", "$end"]

    def test_multi_line(self):
        tokens = list(tokenise(io.StringIO("$timescale\n1ns\n$end")))
        assert tokens == ["$timescale", "1ns", "$end"]

    def test_empty_stream(self):
        assert list(tokenise(io.StringIO(""))) == []

    def test_extra_whitespace(self):
        tokens = list(tokenise(io.StringIO("  a   b  \n  c  ")))
        assert tokens == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Header — metadata blocks
# ---------------------------------------------------------------------------

class TestParseHeaderBasic:
    VCD = """\
$date Tue Apr 14 17:15:11 2026 $end
$version Icarus Verilog $end
$timescale 1ps $end
$enddefinitions $end
"""

    def test_date(self):
        assert _parse(self.VCD).date == "Tue Apr 14 17:15:11 2026"

    def test_version(self):
        assert _parse(self.VCD).version == "Icarus Verilog"

    def test_timescale(self):
        assert _parse(self.VCD).timescale == "1ps"

    def test_no_signals(self):
        model = _parse(self.VCD)
        assert model.signals == {}
        assert model.codes == {}

    def test_multiword_timescale(self):
        vcd = "$timescale 10 ns $end\n$enddefinitions $end\n"
        assert _parse(vcd).timescale == "10 ns"


# ---------------------------------------------------------------------------
# Header — single scope / variable declarations
# ---------------------------------------------------------------------------

class TestParseSingleScope:
    VCD = """\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var reg  4 " cnt [3:0] $end
$upscope $end
$enddefinitions $end
"""

    def test_signal_count(self):
        assert len(_parse(self.VCD).signals) == 2

    def test_clk_path(self):
        model = _parse(self.VCD)
        assert "tb.clk" in model.signals

    def test_clk_attributes(self):
        sig = _parse(self.VCD).signals["tb.clk"]
        assert sig.code == "!"
        assert sig.width == 1
        assert sig.kind == "wire"

    def test_cnt_path_strips_bus_range(self):
        # "[3:0]" must NOT appear in the path
        model = _parse(self.VCD)
        assert "tb.cnt" in model.signals
        assert not any("[" in p for p in model.signals)

    def test_cnt_width(self):
        assert _parse(self.VCD).signals["tb.cnt"].width == 4

    def test_codes_populated(self):
        model = _parse(self.VCD)
        assert "!" in model.codes
        assert '"' in model.codes


# ---------------------------------------------------------------------------
# Header — nested scopes and path assembly
# ---------------------------------------------------------------------------

class TestParseNestedScopes:
    VCD = """\
$timescale 1ns $end
$scope module top $end
$var wire 1 ! a $end
$scope module sub $end
$var wire 1 " b $end
$scope module leaf $end
$var wire 1 # c $end
$upscope $end
$upscope $end
$upscope $end
$enddefinitions $end
"""

    def test_three_level_path(self):
        model = _parse(self.VCD)
        assert "top.a" in model.signals
        assert "top.sub.b" in model.signals
        assert "top.sub.leaf.c" in model.signals

    def test_scope_stack_unwinds(self):
        """After all $upscope tokens the stack must be empty."""
        model = _parse(self.VCD)
        assert len(model.signals) == 3

    def test_bus_range_attached_to_name(self):
        """Handle 'i[31:0]' as a single token — strip the suffix."""
        vcd = (
            "$timescale 1ns $end\n"
            "$scope module tb $end\n"
            "$var integer 32 - i[31:0] $end\n"
            "$upscope $end\n"
            "$enddefinitions $end\n"
        )
        model = _parse(vcd)
        assert "tb.i" in model.signals
        assert model.signals["tb.i"].width == 32


# ---------------------------------------------------------------------------
# Header — identifier code aliasing
# ---------------------------------------------------------------------------

class TestParseAliasCodes:
    VCD = """\
$timescale 1ns $end
$scope module top $end
$var wire 1 ! out $end
$scope module sub $end
$var wire 1 ! out $end
$upscope $end
$upscope $end
$enddefinitions $end
"""

    def test_both_paths_registered(self):
        model = _parse(self.VCD)
        assert "top.out" in model.signals
        assert "top.sub.out" in model.signals

    def test_code_maps_to_two_signals(self):
        model = _parse(self.VCD)
        assert len(model.codes["!"]) == 2

    def test_changes_propagate_to_both(self):
        vcd = self.VCD + "#0\n$dumpvars\n0!\n$end\n#5\n1!\n"
        model = _parse(vcd)
        for sig in model.codes["!"]:
            assert sig.changes == [(0, "0"), (5, "1")]


# ---------------------------------------------------------------------------
# Body — scalar value changes (0/1/x/z)
# ---------------------------------------------------------------------------

class TestParseScalarChanges:
    VCD_HEADER = """\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! sig $end
$upscope $end
$enddefinitions $end
"""

    def _changes(self, body):
        return _parse(self.VCD_HEADER + body).signals["tb.sig"].changes

    def test_zero(self):
        assert self._changes("#0\n0!\n") == [(0, "0")]

    def test_one(self):
        assert self._changes("#0\n1!\n") == [(0, "1")]

    def test_x(self):
        assert self._changes("#0\nx!\n") == [(0, "x")]

    def test_z(self):
        assert self._changes("#0\nz!\n") == [(0, "z")]

    def test_uppercase_X(self):
        """Uppercase X must be treated as 'x'."""
        assert self._changes("#0\nX!\n") == [(0, "x")]

    def test_uppercase_Z(self):
        assert self._changes("#0\nZ!\n") == [(0, "z")]

    def test_multiple_timestamps(self):
        body = "#0\n0!\n#5\n1!\n#10\n0!\n"
        assert self._changes(body) == [(0, "0"), (5, "1"), (10, "0")]

    def test_timestamp_updated_correctly(self):
        """Changes at the same logical time share the same integer time."""
        body = "#100\n0!\n#200\n1!\n"
        changes = self._changes(body)
        assert changes[0][0] == 100
        assert changes[1][0] == 200


# ---------------------------------------------------------------------------
# Body — vector (b-format) and real (r-format) changes
# ---------------------------------------------------------------------------

class TestParseVectorChanges:
    VCD = """\
$timescale 1ns $end
$scope module tb $end
$var reg 4 ! cnt [3:0] $end
$upscope $end
$enddefinitions $end
#0
$dumpvars
b0 !
$end
#10
b0001 !
#20
b1010 !
#30
b1111 !
"""

    def test_initial_value(self):
        changes = _parse(self.VCD).signals["tb.cnt"].changes
        assert changes[0] == (0, "0")

    def test_vector_values_stored_as_bit_strings(self):
        changes = _parse(self.VCD).signals["tb.cnt"].changes
        assert (10, "0001") in changes
        assert (20, "1010") in changes
        assert (30, "1111") in changes

    def test_xz_vector(self):
        vcd = (
            "$timescale 1ns $end\n$scope module tb $end\n"
            "$var reg 4 ! sig [3:0] $end\n$upscope $end\n"
            "$enddefinitions $end\n#0\nbx !\n"
        )
        changes = _parse(vcd).signals["tb.sig"].changes
        assert changes[0] == (0, "x")

    def test_real_change(self):
        vcd = (
            "$timescale 1ns $end\n$scope module tb $end\n"
            "$var real 64 ! v $end\n$upscope $end\n"
            "$enddefinitions $end\n#0\nr3.14 !\n"
        )
        changes = _parse(vcd).signals["tb.v"].changes
        assert changes[0] == (0, "3.14")


# ---------------------------------------------------------------------------
# Body — $dumpvars block (initial values)
# ---------------------------------------------------------------------------

class TestParseDumpvarsBlock:
    VCD = """\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var reg  1 " q   $end
$upscope $end
$enddefinitions $end
#0
$dumpvars
0!
0"
$end
#5
1!
"""

    def test_initial_values_in_dumpvars(self):
        model = _parse(self.VCD)
        assert model.signals["tb.clk"].changes[0] == (0, "0")
        assert model.signals["tb.q"].changes[0] == (0, "0")

    def test_change_after_dumpvars(self):
        model = _parse(self.VCD)
        assert (5, "1") in model.signals["tb.clk"].changes

    def test_dumpall_block_transparent(self):
        """An empty $dumpall $end block before #0 must not cause an error."""
        vcd = (
            "$timescale 1ns $end\n"
            "$scope module tb $end\n$var wire 1 ! clk $end\n$upscope $end\n"
            "$enddefinitions $end\n"
            "$dumpall\n$end\n"
            "#0\n$dumpvars\n0!\n$end\n#5\n1!\n"
        )
        model = _parse(vcd)
        assert model.signals["tb.clk"].changes == [(0, "0"), (5, "1")]

    def test_dumpon_dumpoff_transparent(self):
        """$dumpoff / $dumpon are treated as transparent block markers."""
        vcd = (
            "$timescale 1ns $end\n$scope module tb $end\n"
            "$var wire 1 ! sig $end\n$upscope $end\n"
            "$enddefinitions $end\n"
            "#0\n$dumpvars\n1!\n$end\n"
            "#5\n$dumpoff\nx!\n$end\n"
            "#10\n$dumpon\n1!\n$end\n"
        )
        model = _parse(vcd)
        changes = model.signals["tb.sig"].changes
        assert (5, "x") in changes
        assert (10, "1") in changes


# ---------------------------------------------------------------------------
# Tolerant parsing — unknown keywords
# ---------------------------------------------------------------------------

class TestParseUnknownKeyword:
    def test_unknown_header_keyword_warns(self):
        vcd = (
            "$timescale 1ns $end\n"
            "$unknownthing foo bar $end\n"
            "$enddefinitions $end\n"
        )
        _, msgs = _parse_warn(vcd)
        assert any("Unknown" in m and "unknownthing" in m for m in msgs)

    def test_unknown_header_keyword_skipped(self):
        """The converter must not crash; signals after the unknown block parse fine."""
        vcd = (
            "$timescale 1ns $end\n"
            "$scope module tb $end\n"
            "$var wire 1 ! clk $end\n"
            "$upscope $end\n"
            "$ivl_timescale 1 1 $end\n"
            "$enddefinitions $end\n"
        )
        model, _ = _parse_warn(vcd)
        assert "tb.clk" in model.signals

    def test_unknown_body_keyword_warns(self):
        vcd = (
            "$timescale 1ns $end\n$enddefinitions $end\n"
            "#0\n$mystuff garbage $end\n"
        )
        _, msgs = _parse_warn(vcd)
        assert any("mystuff" in m for m in msgs)

    def test_undeclared_code_warns(self):
        """A value change for a code that was never declared must warn."""
        vcd = "$timescale 1ns $end\n$enddefinitions $end\n#0\n0!\n"
        _, msgs = _parse_warn(vcd)
        assert any("undeclared" in m.lower() for m in msgs)

    def test_undeclared_code_does_not_raise(self):
        vcd = "$timescale 1ns $end\n$enddefinitions $end\n#0\n0!\n"
        model, _ = _parse_warn(vcd)
        assert model is not None


# ---------------------------------------------------------------------------
# Non-standard Icarus Verilog scope name
# ---------------------------------------------------------------------------

class TestIcarusScopeName:
    VCD = """\
$timescale 1ps $end
$scope module tb $end
$scope begin $ivl_for_loop0 $end
$var integer 32 - i [31:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
"""

    def test_ivl_scope_parsed_as_name(self):
        model = _parse(self.VCD)
        assert "tb.$ivl_for_loop0.i" in model.signals

    def test_ivl_scope_width(self):
        model = _parse(self.VCD)
        assert model.signals["tb.$ivl_for_loop0.i"].width == 32


# ---------------------------------------------------------------------------
# Error cases — VCDParseError
# ---------------------------------------------------------------------------

class TestParseMalformedRaises:
    def test_bad_var_width(self):
        vcd = (
            "$timescale 1ns $end\n$scope module tb $end\n"
            "$var wire notanint ! clk $end\n$upscope $end\n"
            "$enddefinitions $end\n"
        )
        with pytest.raises(VCDParseError, match="not an integer|invalid"):
            _parse(vcd)

    def test_bad_timestamp(self):
        vcd = (
            "$timescale 1ns $end\n$scope module tb $end\n"
            "$var wire 1 ! clk $end\n$upscope $end\n"
            "$enddefinitions $end\n"
            "#notanumber\n"
        )
        with pytest.raises(VCDParseError, match="timestamp"):
            _parse(vcd)

    def test_unexpected_eof_in_scope(self):
        vcd = "$timescale 1ns $end\n$scope module tb\n"
        with pytest.raises(VCDParseError):
            _parse(vcd)

    def test_unexpected_eof_in_var(self):
        vcd = (
            "$timescale 1ns $end\n$scope module tb $end\n"
            "$var wire 1\n"
        )
        with pytest.raises(VCDParseError):
            _parse(vcd)


# ---------------------------------------------------------------------------
# Full round-trip — fixture files
# ---------------------------------------------------------------------------

class TestFixtures:
    def test_simple_vcd_signal_count(self):
        model = parse(FIXTURES / "simple.vcd")
        assert len(model.signals) == 2

    def test_simple_vcd_timescale(self):
        model = parse(FIXTURES / "simple.vcd")
        assert model.timescale == "1ns"

    def test_simple_vcd_clk_changes(self):
        model = parse(FIXTURES / "simple.vcd")
        clk = model.signals["tb.clk"]
        assert clk.changes == [(0,"0"),(5,"1"),(10,"0"),(15,"1"),(20,"0"),(25,"1")]

    def test_simple_vcd_q_changes(self):
        model = parse(FIXTURES / "simple.vcd")
        q = model.signals["tb.q"]
        assert q.changes == [(0, "0"), (10, "1"), (20, "0")]

    def test_example_vcd_timescale(self):
        model = parse(FIXTURES / "example.vcd")
        assert model.timescale == "1ps"

    def test_example_vcd_version(self):
        model = parse(FIXTURES / "example.vcd")
        assert "Icarus" in model.version

    def test_example_vcd_signal_count(self):
        model = parse(FIXTURES / "example.vcd")
        # tb_circuit:            5  (out, w, x, y, z)
        # tb_circuit.uut:       12  (7 unique + 5 aliases of tb_circuit signals)
        # tb_circuit.$ivl_…:     1  (i [31:0])
        assert len(model.signals) == 18

    def test_example_vcd_aliasing(self):
        """Code '!' must map to exactly two Signal objects."""
        model = parse(FIXTURES / "example.vcd")
        assert len(model.codes["!"]) == 2

    def test_example_vcd_alias_same_changes(self):
        """Both aliases of '!' must have identical change lists."""
        model = parse(FIXTURES / "example.vcd")
        sigs = model.codes["!"]
        assert sigs[0].changes == sigs[1].changes

    def test_example_vcd_counter_last_value(self):
        """Counter i should reach 0x10 (16) at the final timestamp."""
        model = parse(FIXTURES / "example.vcd")
        counter = model.signals["tb_circuit.$ivl_for_loop0.i"]
        last_time, last_val = counter.changes[-1]
        assert last_time == 160000
        assert last_val == "10000"  # binary for 16

    def test_example_vcd_17_timestamps(self):
        """The VCD has 17 distinct change timestamps (0 … 160000)."""
        model = parse(FIXTURES / "example.vcd")
        times = {t for sig in model.signals.values() for t, _ in sig.changes}
        assert len(times) == 17
