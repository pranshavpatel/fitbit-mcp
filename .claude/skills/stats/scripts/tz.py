"""The timezone every date and clock time is shown in.

First match wins: $FITDASH_TZ, "timezone" in ~/.fitbit-mcp/coaching/stats.json, this computer's own
timezone, then UTC. Names are IANA zones, e.g. "Europe/London" or "America/New_York".
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _system_zone() -> str | None:
    try:
        target = os.path.realpath("/etc/localtime")          # …/zoneinfo/Region/City on macOS and Linux
    except OSError:
        return None
    marker = "zoneinfo/"
    return target.split(marker, 1)[1] if marker in target else None


def local_zone() -> ZoneInfo:
    home = Path(os.environ.get("FITBIT_MCP_HOME", "~/.fitbit-mcp")).expanduser()
    cfg_tz = None
    try:
        cfg_tz = json.loads((home / "coaching" / "stats.json").read_text()).get("timezone")
    except (OSError, ValueError, AttributeError):
        pass
    for name in (os.environ.get("FITDASH_TZ"), cfg_tz, os.environ.get("TZ"), _system_zone(), "UTC"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")
