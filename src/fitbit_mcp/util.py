"""Small shared helpers: atomic private files, time parsing, secret redaction.

`save` and `read_json` are ported from the original exporter (export.py).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("fitbit_mcp")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def private_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def save(path: Path | str, data: Any) -> None:
    """Atomic write with owner-only permissions on POSIX (credentials and health data)."""
    path = Path(path)
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".writing-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data if isinstance(data, bytes) else
                         json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------- time helpers

_OFFSET_RE = re.compile(r"^(-?\d+(?:\.\d+)?)s$")


def parse_rfc3339(value: str | None) -> datetime | None:
    """Parse RFC 3339 timestamps on Python 3.10 (which rejects 'Z' and >6 fraction digits)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", text)
    if match:
        frac = (match.group(2) or "")[:7]  # '.' + max 6 digits
        text = match.group(1) + frac + match.group(3)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed


def to_utc_z(moment: datetime | None) -> str | None:
    if moment is None or moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_offset_seconds(value: Any) -> int | None:
    """Google duration offsets such as '-14400s'."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = _OFFSET_RE.match(value.strip())
        if match:
            return int(float(match.group(1)))
    return None


def civil_to_str(civil: Any) -> tuple[str | None, str | None]:
    """Google CivilDateTime {'date': {'year','month','day'}, 'time': {...}} -> (date, datetime)."""
    if not isinstance(civil, dict):
        return None, None
    d = civil.get("date") if "date" in civil else civil
    try:
        day = date(int(d["year"]), int(d["month"]), int(d["day"])).isoformat()
    except (KeyError, TypeError, ValueError):
        return None, None
    t = civil.get("time") or {}
    if isinstance(t, dict) and t:
        try:
            moment = "{}T{:02d}:{:02d}:{:02d}".format(day, int(t.get("hours", 0)), int(t.get("minutes", 0)),
                                                       int(t.get("seconds", 0)))
            return day, moment
        except (TypeError, ValueError):
            return day, None
    return day, None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value: str | None, field: str = "date") -> date | None:
    """Strict YYYY-MM-DD (calendar-validated, so 2025-02-29 is rejected)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError("{} must be YYYY-MM-DD".format(field))
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("{} is not a valid calendar date".format(field)) from None


def daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def chunks(start: date, end: date, days: int):
    """Inclusive date chunks of at most `days` days, newest first."""
    current_end = end
    while current_end >= start:
        current_start = max(start, current_end - timedelta(days=days - 1))
        yield current_start, current_end
        current_end = current_start - timedelta(days=1)


def to_number(value: Any) -> float | None:
    """Numbers and int64 strings -> float. Booleans, non-numeric strings, NaN and infinity -> None.

    Google sends the literal string "NaN" (e.g. skin-temperature baselines before 30 days of
    history); treating it as a number would corrupt summaries, so it is missing data.
    """
    import math
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


# ------------------------------------------------------------- redaction / logs

_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)((?:access_token|refresh_token|id_token|client_secret|code_verifier|code|token)"
               r"[\"']?\s*[:=]\s*[\"']?)[^\s&\"',}]+"),
    re.compile(r"(?i)(basic\s+)[A-Za-z0-9+/=]+"),
]


def redact(text: Any) -> str:
    text = str(text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        return True


def configure_logging(log_dir: Path | None = None, level: str = "INFO") -> None:
    """Diagnostics go to stderr (and optionally a private log file); never stdout."""
    root = logging.getLogger("fitbit_mcp")
    root.setLevel(level)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    stream.addFilter(RedactingFilter())
    root.addHandler(stream)
    if log_dir is not None:
        from logging.handlers import RotatingFileHandler
        private_dir(log_dir)
        path = log_dir / "server.log"
        file_handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        file_handler.addFilter(RedactingFilter())
        root.addHandler(file_handler)
