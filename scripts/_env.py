"""Shared .env-style config loading for playsai scripts.

Looks for a `.env` file (current directory, then the repo root) and loads
PLAYSAI_*-prefixed KEY=VALUE lines into os.environ, without overwriting
anything already set there. Values are only used to *seed* argparse
defaults — a CLI flag, when passed, always wins, since argparse only falls
back to `default=` when the flag is omitted.

Example .env:
    PLAYSAI_DB=/home/you/.config/beets/musiclibrary.db
    PLAYSAI_LASTFM_KEY=abcdef1234567890
"""
import os
from pathlib import Path

_loaded = False


def _parse_env_file(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]
    for candidate in candidates:
        if candidate.is_file():
            for key, value in _parse_env_file(candidate).items():
                os.environ.setdefault(key, value)
            return


def get(name, default=None):
    """Return PLAYSAI_<name> from the environment or a .env file, else default."""
    _load()
    return os.environ.get(f"PLAYSAI_{name}", default)
