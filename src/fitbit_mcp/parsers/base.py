"""Normalized record model shared by all parsers.

One Rec = one metric observation (a sample, an interval total, a session metric or a
daily value). Original provider objects are kept verbatim in `payload`, and every
record links to the retained raw response file it came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# time_basis values
UTC_WITH_OFFSET = "utc_with_offset"   # absolute instant + the UTC offset the device recorded
LOCAL = "local"                       # provider calendar/wall time, no offset supplied
DATE_ONLY = "date_only"               # a calendar date (daily summaries)
UNKNOWN_ZONE = "unknown_zone"         # naive timestamp whose zone the file does not state


@dataclass
class Rec:
    category: str
    metric: str
    granularity: str           # sample | interval | session | daily
    record_key: str            # stable de-duplication key within (provider, category, metric)
    value: float | None = None
    unit: str | None = None
    value_text: str | None = None
    local_date: str | None = None
    start_local: str | None = None
    end_local: str | None = None
    start_utc: str | None = None
    end_utc: str | None = None
    utc_offset_seconds: int | None = None
    time_basis: str = LOCAL
    source_id: str | None = None
    data_source: dict | None = None
    quality: list[str] = field(default_factory=list)
    payload: Any = None
    scope_key: str | None = None
    details: dict | None = None


@dataclass
class ParseResult:
    records: list[Rec] = field(default_factory=list)
    unsupported: list[tuple[str, str]] = field(default_factory=list)   # (category hint, reason)
    warnings: list[str] = field(default_factory=list)

    def extend(self, other: "ParseResult") -> None:
        self.records.extend(other.records)
        self.unsupported.extend(other.unsupported)
        self.warnings.extend(other.warnings)
