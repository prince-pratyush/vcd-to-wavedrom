"""Integration tests for the vcd2wavedrom CLI.

Every test here invokes the converter as a real subprocess (via
``sys.executable -m vcd2wavedrom.cli``) so the full process boundary —
argument parsing, file I/O, stdout/stderr, exit codes — is exercised.

Tests never inspect internal state; they only observe what the process
writes to stdout/stderr and what exit code it returns.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SIMPLE = FIXTURES / "simple.vcd"
EXAMPLE = FIXTURES / "example.vcd"
SIMPLE_EXPECTED = json.loads((FIXTURES / "simple_expected.json").read_text())
EXAMPLE_EXPECTED = json.loads((FIXTURES / "example_expected.json").read_text())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cli(*args, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run ``vcd2wavedrom`` and return the CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-m", "vcd2wavedrom.cli", *args],
        input=stdin,
        capture_output=True,
        text=True,
    )


def json_stdout(proc: subprocess.CompletedProcess) -> dict:
    """Parse stdout as JSON, with a helpful error if it fails."""
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"stdout is not valid JSON:\n{proc.stdout!r}\nstderr:\n{proc.stderr!r}\n"
            f"JSON error: {exc}"
        )


# ---------------------------------------------------------------------------
# Basic invocation
# ---------------------------------------------------------------------------

class TestBasicInvocation:
    def test_version_flag(self):
        proc = cli("--version")
        assert proc.returncode == 0
        assert "vcd2wavedrom" in proc.stdout
        # Version string must match pyproject.toml
        assert "0.1.0" in proc.stdout

    def test_help_flag(self):
        proc = cli("--help")
        assert proc.returncode == 0
        assert "FILE" in proc.stdout
        assert "--signals" in proc.stdout
        assert "--period" in proc.stdout

    def test_file_mode_exit_zero(self):
        assert cli(str(SIMPLE)).returncode == 0

    def test_file_mode_stdout_is_json(self):
        proc = cli(str(SIMPLE))
        result = json_stdout(proc)
        assert "head" in result
        assert "signal" in result


# ---------------------------------------------------------------------------
# File mode vs stdin mode
# ---------------------------------------------------------------------------

class TestInputModes:
    def test_file_mode_matches_expected(self):
        proc = cli(str(SIMPLE))
        assert json_stdout(proc) == SIMPLE_EXPECTED

    def test_stdin_mode_matches_expected(self):
        vcd_text = SIMPLE.read_text()
        proc = cli(stdin=vcd_text)
        assert json_stdout(proc) == SIMPLE_EXPECTED

    def test_stdin_mode_no_file_arg(self):
        """Passing no positional arg reads from stdin."""
        vcd_text = SIMPLE.read_text()
        proc = cli(stdin=vcd_text)
        assert proc.returncode == 0

    def test_example_file_mode(self):
        proc = cli(str(EXAMPLE))
        assert json_stdout(proc) == EXAMPLE_EXPECTED


# ---------------------------------------------------------------------------
# --pretty
# ---------------------------------------------------------------------------

class TestPrettyFlag:
    def test_pretty_output_is_indented(self):
        proc = cli(str(SIMPLE), "--pretty")
        assert proc.returncode == 0
        # Indented JSON has newlines and leading spaces
        assert "\n  " in proc.stdout

    def test_pretty_parses_as_same_json(self):
        proc = cli(str(SIMPLE), "--pretty")
        assert json_stdout(proc) == SIMPLE_EXPECTED

    def test_default_is_compact(self):
        proc = cli(str(SIMPLE))
        # Compact output has no leading whitespace on internal lines
        lines = proc.stdout.strip().splitlines()
        assert len(lines) == 1  # entire JSON on one line


# ---------------------------------------------------------------------------
# --signals filter
# ---------------------------------------------------------------------------

class TestSignalsFilter:
    def test_single_signal(self):
        proc = cli(str(SIMPLE), "-s", "clk")
        result = json_stdout(proc)
        assert len(result["signal"]) == 1
        assert result["signal"][0]["name"] == "tb.clk"

    def test_multiple_signals_csv(self):
        proc = cli(str(SIMPLE), "-s", "clk,q")
        result = json_stdout(proc)
        names = [s["name"] for s in result["signal"]]
        assert "tb.clk" in names
        assert "tb.q" in names

    def test_full_path_match(self):
        proc = cli(str(SIMPLE), "--signals", "tb.clk")
        result = json_stdout(proc)
        assert result["signal"][0]["name"] == "tb.clk"

    def test_unmatched_signal_warning_on_stderr(self):
        proc = cli(str(SIMPLE), "-s", "doesnotexist")
        assert proc.returncode == 0
        assert "warning" in proc.stderr.lower()
        assert "doesnotexist" in proc.stderr

    def test_unmatched_signal_still_exits_zero(self):
        proc = cli(str(SIMPLE), "-s", "doesnotexist")
        assert proc.returncode == 0


# ---------------------------------------------------------------------------
# --period sampling
# ---------------------------------------------------------------------------

class TestPeriodSampling:
    def test_period_changes_tick_count(self):
        # simple.vcd has 6 event-driven ticks (0,5,10,15,20,25)
        # With period=10: ticks at 0,10,20 → 3 ticks
        proc = cli(str(SIMPLE), "-p", "10")
        result = json_stdout(proc)
        clk = next(s for s in result["signal"] if s["name"] == "tb.clk")
        assert len(clk["wave"]) == 3

    def test_period_output_valid_json(self):
        proc = cli(str(SIMPLE), "-p", "5")
        assert proc.returncode == 0
        json_stdout(proc)

    def test_period_zero_exits_nonzero(self):
        proc = cli(str(SIMPLE), "-p", "0")
        assert proc.returncode != 0
        assert "error" in proc.stderr.lower()

    def test_period_negative_exits_nonzero(self):
        proc = cli(str(SIMPLE), "-p", "-10")
        assert proc.returncode != 0

    def test_period_error_message_on_stderr(self):
        proc = cli(str(SIMPLE), "-p", "0")
        assert "period" in proc.stderr.lower()


# ---------------------------------------------------------------------------
# --group
# ---------------------------------------------------------------------------

class TestGroupFlag:
    def test_group_produces_nested_list(self):
        proc = cli(str(SIMPLE), "-g")
        result = json_stdout(proc)
        # With --group, the "tb" scope becomes a list entry
        groups = [s for s in result["signal"] if isinstance(s, list)]
        assert len(groups) >= 1

    def test_group_label_is_scope_name(self):
        proc = cli(str(SIMPLE), "-g")
        result = json_stdout(proc)
        groups = [s for s in result["signal"] if isinstance(s, list)]
        assert groups[0][0] == "tb"

    def test_group_exit_zero(self):
        assert cli(str(SIMPLE), "--group").returncode == 0


# ---------------------------------------------------------------------------
# --output flag
# ---------------------------------------------------------------------------

class TestOutputFlag:
    def test_output_to_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_path = f.name
        proc = cli(str(SIMPLE), "-o", out_path)
        assert proc.returncode == 0
        content = json.loads(Path(out_path).read_text())
        assert content == SIMPLE_EXPECTED

    def test_output_file_stdout_is_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_path = f.name
        proc = cli(str(SIMPLE), "-o", out_path)
        assert proc.stdout.strip() == ""

    def test_stdout_is_clean_json(self):
        """Warnings go to stderr; stdout must be parseable JSON."""
        proc = cli(str(SIMPLE))
        # If there were any warnings, they go to stderr, not stdout
        json_stdout(proc)  # raises if stdout is not valid JSON


# ---------------------------------------------------------------------------
# stderr cleanliness
# ---------------------------------------------------------------------------

class TestStderrBehaviour:
    def test_no_warnings_on_clean_vcd(self):
        """simple.vcd is well-formed; stderr must be empty."""
        proc = cli(str(SIMPLE))
        assert proc.stderr == ""

    def test_stdout_unaffected_by_warnings(self):
        """Even with warnings, stdout must be valid JSON."""
        # Inject a VCD with an unknown keyword to trigger a warning
        vcd = (
            "$timescale 1ns $end\n"
            "$scope module tb $end\n$var wire 1 ! clk $end\n$upscope $end\n"
            "$ivl_timescale 1 1 $end\n"
            "$enddefinitions $end\n#0\n$dumpvars\n0!\n$end\n"
        )
        proc = cli(stdin=vcd)
        assert proc.returncode == 0
        assert proc.stderr != ""          # warning present
        json_stdout(proc)                 # stdout still valid JSON

    def test_warnings_prefixed_correctly(self):
        vcd = (
            "$timescale 1ns $end\n"
            "$scope module tb $end\n$var wire 1 ! clk $end\n$upscope $end\n"
            "$unknownkw stuff $end\n"
            "$enddefinitions $end\n#0\n$dumpvars\n0!\n$end\n"
        )
        proc = cli(stdin=vcd)
        assert "vcd2wavedrom: warning:" in proc.stderr


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_nonexistent_file_exits_nonzero(self):
        proc = cli("/tmp/file_that_does_not_exist_xyz.vcd")
        assert proc.returncode != 0

    def test_nonexistent_file_error_on_stderr(self):
        proc = cli("/tmp/file_that_does_not_exist_xyz.vcd")
        assert "error" in proc.stderr.lower()

    def test_nonexistent_file_empty_stdout(self):
        proc = cli("/tmp/file_that_does_not_exist_xyz.vcd")
        assert proc.stdout.strip() == ""

    def test_malformed_vcd_exits_nonzero(self):
        bad_vcd = "$var wire notanint ! clk $end\n$enddefinitions $end\n"
        proc = cli(stdin=bad_vcd)
        assert proc.returncode != 0

    def test_malformed_vcd_error_on_stderr(self):
        bad_vcd = "$var wire notanint ! clk $end\n$enddefinitions $end\n"
        proc = cli(stdin=bad_vcd)
        assert "error" in proc.stderr.lower()

    def test_error_message_prefixed_correctly(self):
        proc = cli("/tmp/no_such_file.vcd")
        assert "vcd2wavedrom: error:" in proc.stderr


# ---------------------------------------------------------------------------
# End-to-end round-trip with example.vcd
# ---------------------------------------------------------------------------

class TestExampleRoundTrip:
    def test_exact_json_match(self):
        proc = cli(str(EXAMPLE))
        assert json_stdout(proc) == EXAMPLE_EXPECTED

    def test_no_stderr_on_example(self):
        """example.vcd is the task-brief VCD — no warnings expected."""
        proc = cli(str(EXAMPLE))
        assert proc.stderr == ""

    def test_signals_filter_with_example(self):
        proc = cli(str(EXAMPLE), "-s", "w,x,y,z,out")
        result = json_stdout(proc)
        # Each of the 5 names matches 2 aliases → 10 entries
        assert len(result["signal"]) == 10

    def test_group_with_example(self):
        proc = cli(str(EXAMPLE), "-g", "--pretty")
        result = json_stdout(proc)
        groups = [s for s in result["signal"] if isinstance(s, list)]
        assert len(groups) >= 1
