"""Scanner modules (spec 3.1-3.7).

Each scanner is a pure function of one symbol's bars plus context. They are
registered here so the runner and the UI can enumerate them without importing
each module by hand.
"""

from infinity.scanners.base import (
    REGISTRY,
    ScanContext,
    Scanner,
    ScanRow,
    Signal,
    get_scanner,
    register,
)

# Importing for the side effect of registering each scanner.
from infinity.scanners import (  # noqa: E402,F401  isort:skip
    candle_50,
    displacement,
    golden_zone,
    resistance_breakout,
    trendlines,
    triangle,
)

__all__ = [
    "REGISTRY",
    "ScanContext",
    "ScanRow",
    "Scanner",
    "Signal",
    "get_scanner",
    "register",
]
