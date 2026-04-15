"""vcd2wavedrom — VCD to WaveDrom WaveJSON converter."""

__version__ = "0.1.0"

from .model import Signal, VCDModel, VCDError, VCDParseError, VCDEmitError, VCDWarning
from .parser import parse
from .emitter import emit

__all__ = [
    "__version__",
    "Signal",
    "VCDModel",
    "VCDError",
    "VCDParseError",
    "VCDEmitError",
    "VCDWarning",
    "parse",
    "emit",
]
