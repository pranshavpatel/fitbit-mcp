"""Validated, parameterized read access to local records (no free-form SQL)."""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from typing import Any

from . import catalog
from .config import Settings
from .store import Store, decode_cursor, encode_cursor

PROVIDERS = ("fitbit", "google", "fitbit_takeout", "google_takeout")
GRANULARITIES = ("sample", "interval", "session", "daily")
_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,64}$")
MAX_QUERY_LIMIT = 500
MAX_EXPORT_ROWS = 2_000_000
PAYLOAD_CHAR_LIMIT = 4000


def _validate_name(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not _NAME_RE.match(value):
        raise ValueError("{} may contain only lowercase letters, digits, '_' and '-'".format(field))
    return value


def build_filters(store: Store, *, category: str | None = None, metric: str | None = None,
                  start_date: str | None = None, end_date: str | None = None, provider: str | None = None,
                  granularity: str | None = None, min_value: float | None = None, max_value: float | None = None,
                  include_deleted: bool = False) -> tuple[str, list[Any]]:
    from .util import parse_date
    clauses, args = [], []
    category, metric = _validate_name(category, "category"), _validate_name(metric, "metric")
    if provider is not None and provider not in PROVIDERS:
        raise ValueError("provider must be one of " + ", ".join(PROVIDERS))
    if granularity is not None and granularity not in GRANULARITIES:
        raise ValueError("granularity must be one of " + ", ".join(GRANULARITIES))
    start, end = parse_date(start_date, "start_date"), parse_date(end_date, "end_date")
    if start and end and start > end:
        raise ValueError("start_date must be on or before end_date")
    if min_value is not None and max_value is not None and min_value > max_value:
        raise ValueError("min_value must be <= max_value")
    if category is not None:
        with store.connect() as conn:
            known = conn.execute("SELECT 1 FROM records WHERE category=? LIMIT 1", (category,)).fetchone()
        if not known:
            raise ValueError("No local records in category '{}'. Use list_data_types to see what is "
                             "available.".format(category))
    for column, value in (("category", category), ("metric", metric), ("provider", provider),
                          ("granularity", granularity)):
        if value is not None:
            clauses.append(column + " = ?")
            args.append(value)
    if start:
        clauses.append("local_date >= ?")
        args.append(start.isoformat())
    if end:
        clauses.append("local_date <= ?")
        args.append(end.isoformat())
    if min_value is not None:
        clauses.append("value >= ?")
        args.append(float(min_value))
    if max_value is not None:
        clauses.append("value <= ?")
        args.append(float(max_value))
    if not include_deleted:
        clauses.append("status = 'active'")
    else:
        clauses.append("status != 'excluded_catalog'")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


_COLUMNS = ("r.id, r.provider, r.category, r.metric, r.granularity, r.local_date, r.start_local, r.end_local, "
            "r.start_utc, r.end_utc, r.utc_offset_seconds, r.time_basis, r.value, r.value_text, r.unit, r.source_id, "
            "r.data_source, r.quality, r.details, r.status, r.revision, r.first_seen, r.updated_at, r.raw_file_id, "
            "r.parser")


def _row(row, payload: str | None = None) -> dict[str, Any]:
    item = {k: row[k] for k in row.keys() if k != "payload_json"}
    for key in ("data_source", "quality", "details"):
        if item.get(key):
            item[key] = json.loads(item[key])
    item = {k: v for k, v in item.items() if v is not None}
    if payload is not None:
        item["original"] = json.loads(payload) if len(payload) <= PAYLOAD_CHAR_LIMIT else \
            {"truncated": True, "chars": len(payload), "hint": "use export_data with include_original=true"}
    return item


def query(store: Store, *, limit: int = 100, cursor: str | None = None, order: str = "asc",
          include_original: bool = False, **filters: Any) -> dict[str, Any]:
    if not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError("limit must be between 1 and {}".format(MAX_QUERY_LIMIT))
    if order not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    where, args = build_filters(store, **filters)
    key_where, key_args = "", []
    if cursor:
        position = decode_cursor(cursor)
        if position.get("order") != order:
            raise ValueError("cursor was created with a different order")
        comparator = ">" if order == "asc" else "<"
        key_where = " AND (COALESCE(local_date,''), id) {} (?, ?)".format(comparator)
        key_args = [position["d"], position["id"]]
        if not where:
            key_where = " WHERE" + key_where[4:]
    direction = "ASC" if order == "asc" else "DESC"
    sql = ("SELECT " + _COLUMNS + (", p.json AS payload_json" if include_original else "") +
           " FROM (SELECT * FROM records" + where + key_where + " ORDER BY COALESCE(local_date,'') " + direction +
           ", id " + direction + " LIMIT ?) r" +
           (" LEFT JOIN payloads p ON p.id = r.payload_id" if include_original else "") +
           " ORDER BY COALESCE(r.local_date,'') " + direction + ", r.id " + direction)
    with store.connect() as conn:
        rows = conn.execute(sql, args + key_args + [limit + 1]).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM records" + where + " LIMIT 100001)",
                             args).fetchone()[0]
    more = len(rows) > limit
    rows = rows[:limit]
    items = [_row(r, r["payload_json"] if include_original else None) for r in rows]
    result: dict[str, Any] = {"count": len(items), "total_matching": total if total <= 100000 else ">100000",
                              "records": items}
    if more and rows:
        last = rows[-1]
        result["next_cursor"] = encode_cursor({"d": last["local_date"] or "", "id": last["id"], "order": order})
    result["notes"] = ["local_date is the provider's calendar date for the record; time_basis explains whether "
                       "timestamps carry a UTC offset.",
                       "Records marked deleted_upstream are hidden unless include_deleted=true."]
    return result


def export(store: Store, settings: Settings, *, fmt: str, include_original: bool = False,
           **filters: Any) -> dict[str, Any]:
    if fmt not in ("json", "csv"):
        raise ValueError("format must be 'json' or 'csv'")
    where, args = build_filters(store, **filters)
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM records" + where, args).fetchone()[0]
    if count == 0:
        raise ValueError("No records match these filters; nothing exported.")
    if count > MAX_EXPORT_ROWS:
        raise ValueError("{} records match; narrow the filters (limit {}).".format(count, MAX_EXPORT_ROWS))
    label = "-".join(x for x in (filters.get("provider"), filters.get("category"), filters.get("metric")) if x) or "all"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = settings.exports_dir / "fitbit-{}-{}.{}".format(label, stamp, fmt)
    columns = [c.split(".")[1] for c in _COLUMNS.split(", ")]
    sql = ("SELECT " + _COLUMNS + (", p.json AS payload_json" if include_original else "") + " FROM records r" +
           (" LEFT JOIN payloads p ON p.id = r.payload_id" if include_original else "") +
           where +  # payloads has no columns named like the record filters, so they stay unambiguous
           " ORDER BY COALESCE(r.local_date,''), r.id")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    written = 0
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle, store.connect() as conn:
        cursor = conn.execute(sql, args)
        if fmt == "csv":
            writer = csv.writer(handle)
            writer.writerow(columns + (["original_json"] if include_original else []))
            for row in cursor:
                writer.writerow([row[c] for c in columns] + ([row["payload_json"]] if include_original else []))
                written += 1
        else:
            handle.write('{"exported_at": "%s", "filters": %s, "records": [\n' % (
                datetime.now().astimezone().isoformat(), json.dumps({k: v for k, v in filters.items() if v is not None})))
            for row in cursor:
                item = {c: row[c] for c in columns}
                for key in ("data_source", "quality", "details"):
                    if item.get(key):
                        item[key] = json.loads(item[key])
                if include_original and row["payload_json"]:
                    item["original"] = json.loads(row["payload_json"])
                handle.write((",\n" if written else "") + json.dumps(item, ensure_ascii=False))
                written += 1
            handle.write("\n]}\n")
    return {"path": str(path), "format": fmt, "records": written, "bytes": path.stat().st_size,
            "note": "File is stored locally with owner-only permissions; it is not uploaded anywhere."}


def data_types(store: Store) -> dict[str, Any]:
    with store.connect() as conn:
        metrics = [dict(r) for r in conn.execute(
            "SELECT provider, category, metric, granularity, unit, COUNT(*) AS records, "
            "SUM(CASE WHEN status='deleted_upstream' THEN 1 ELSE 0 END) AS deleted_upstream, "
            "MIN(local_date) AS first_date, MAX(local_date) AS last_date, "
            "COUNT(DISTINCT local_date) AS days_with_data FROM records WHERE status != 'excluded_catalog' "
            "GROUP BY provider, category, metric, granularity, unit ORDER BY provider, category, metric")]
        statuses = [dict(r) for r in conn.execute(
            "SELECT provider, grp AS request_group, category, status, reason, updated_at FROM category_status "
            "WHERE status != 'ok' ORDER BY provider, category, grp")]
        unsupported = [dict(r) for r in conn.execute(
            "SELECT provider, category_hint, reason, COUNT(*) AS files_or_requests, SUM(occurrences) AS occurrences "
            "FROM unsupported GROUP BY provider, category_hint, reason ORDER BY files_or_requests DESC LIMIT 40")]
        imported = {(r["provider"], r["category"]) for r in conn.execute(
            "SELECT DISTINCT provider, category FROM records")}
        sync = [dict(r) for r in conn.execute("SELECT provider, last_synced_through, last_sync_at FROM sync_state")]
    for item in metrics:
        if item["unit"] in (None, "unknown") or str(item["unit"]).startswith("unknown"):
            item["note"] = "unit unknown; excluded from unit-converted summaries"
    not_imported = {
        "fitbit": [c for c in catalog.FITBIT_CATEGORIES if ("fitbit", c) not in imported],
        "google": [c for c in catalog.GOOGLE_CATEGORIES if ("google", c) not in imported],
    }
    return {"metrics": metrics, "missing_denied_or_failed": statuses, "categories_without_local_records":
            not_imported, "unsupported_or_unparsed": unsupported, "sync_state": sync,
            "notes": ["A category with no local records may be not yet imported, not granted, denied, or genuinely "
                      "empty; see missing_denied_or_failed for the recorded reason.",
                      "Unsupported items are retained on disk (raw responses or extracted Takeout files)."]}
