"""Shared import paths for the independently delivered Agent v2 test lines."""

from __future__ import annotations

import sys
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
SERVER_ROOT = TEST_ROOT.parents[1]
for path in (SERVER_ROOT, TEST_ROOT / "api", TEST_ROOT / "runtime", TEST_ROOT / "tools"):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)
