"""Minimal `.env` loader (stdlib only).

`load_dotenv()` reads KEY=VALUE lines from a `.env` file and puts them into
`os.environ` **without overriding** variables that are already set, so a key
exported in the shell always wins over the file. Comments (`#`), blank lines,
an optional `export ` prefix and single/double quotes around the value are
handled. Nothing else: no interpolation, no multi-line values.

Search order when no path is given: `.env` in the current working directory,
then `.env` in the repository root (the parent of this package).
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # trailing comment on an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def load_dotenv(path: str | os.PathLike[str] | None = None, override: bool = False) -> dict[str, str]:
    """Load `.env` into os.environ. Returns the variables that were actually set."""
    candidates = [Path(path)] if path else [Path.cwd() / ".env", REPO_ROOT / ".env"]
    for p in candidates:
        if p.is_file():
            loaded: dict[str, str] = {}
            for k, v in parse_dotenv(p.read_text(encoding="utf-8")).items():
                if override or k not in os.environ:
                    os.environ[k] = v
                    loaded[k] = v
            return loaded
    return {}
