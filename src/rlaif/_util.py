"""Internal shared helpers. Nothing user-facing.

Keep this leaf-only — anything that wants to import from a sibling rlaif
module belongs in that module instead. The point is to consolidate the
truly trivial duplicates that otherwise fan out across CLI subcommands.
"""

from __future__ import annotations

import json
from typing import Any


def pretty_json(obj: Any) -> str:
    """Two-space-indented JSON, ``str``-fallback for non-serializable values."""
    return json.dumps(obj, indent=2, default=str)
