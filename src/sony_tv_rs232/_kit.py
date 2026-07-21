"""Single import seam for serialkit.

Every other module imports serialkit names from here, so vendoring the driver
into a Home Assistant integration only has to rewrite this one line (from the
top-level ``serialkit`` package to the integration's vendored relative copy,
e.g. ``from ..serialkit import ...``). Keeping the seam in one place is what
makes the "vendor an unreleased dependency" story a one-file change.
"""

from __future__ import annotations

from serialkit import (
    CommandTimeoutError,
    ConnectionLostError,
    Pacing,
    ProtocolError,
    ResyncError,
    SerialDevice,
    SerialKitError,
)

__all__ = [
    "CommandTimeoutError",
    "ConnectionLostError",
    "Pacing",
    "ProtocolError",
    "ResyncError",
    "SerialDevice",
    "SerialKitError",
]
