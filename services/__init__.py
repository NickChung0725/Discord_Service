"""Discord Bot slash commands and shared external service integrations (AI, Weather, GitHub Pages)."""

from __future__ import annotations

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
for _sub in (_ROOT, _ROOT / "services", _ROOT / "quant", _ROOT / "taishin"):
    _p = str(_sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)
