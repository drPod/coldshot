from __future__ import annotations

import functools
import tomllib
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
_CONFIG_PATH = _PROJECT_ROOT / "coldshot.toml"


@functools.cache
def load() -> dict:
    """Load coldshot.toml. Cached after first call."""
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config not found: {_CONFIG_PATH}\n"
            "Run: cp coldshot.example.toml coldshot.toml"
        )
    with open(_CONFIG_PATH, "rb") as f:
        return tomllib.load(f)
