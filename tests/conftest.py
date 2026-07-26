"""Make `proc_warden` importable from a plain checkout.

CI installs the package (`pip install -e .`), but a clone with nothing installed
should still be able to run `pytest`, because the integration tests drive the
`proc` shim from the repo root anyway.
"""

from __future__ import annotations

import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
