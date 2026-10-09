"""SQLite storage. Every statement is parameterized; no caller-supplied SQL is ever executed."""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .parsers.base import Rec
from .util import canonical_json, private_dir, save, sha256_bytes, utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS raw_files (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    origin TEXT NOT NULL,            -- 'api' | 'takeout'
    grp TEXT,
    endpoint TEXT,                   -- API path or archive member path
    params_json TEXT,                -- request parameters (never credentials)
    locale TEXT,
    fetched_at TEXT NOT NULL,
    path TEXT NOT NULL,              -- retained original bytes, relative to the data home
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    job_id TEXT
);

CREATE TABLE IF NOT EXISTS payloads (id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    category TEXT NOT NULL,
    metric TEXT NOT NULL,
    granularity TEXT NOT NULL,
    record_key TEXT NOT NULL,
    scope_key TEXT,
    local_date TEXT,
    start_local TEXT, end_local TEXT,
    start_utc TEXT, end_utc TEXT,
    utc_offset_seconds INTEGER,
    time_basis TEXT NOT NULL,
    value REAL, value_text TEXT, unit TEXT,
    source_id TEXT, data_source TEXT,
    quality TEXT, details TEXT,
    payload_id INTEGER REFERENCES payloads(id),
    raw_file_id INTEGER REFERENCES raw_files(id),
    parser TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',      -- active | deleted_upstream
    fingerprint TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    UNIQUE (provider, category, metric, record_key)
);
CREATE INDEX IF NOT EXISTS idx_records_lookup ON records(category, metric, local_date);
CREATE INDEX IF NOT EXISTS idx_records_provider_date ON records(provider, local_date);
CREATE INDEX IF NOT EXISTS idx_records_scope ON records(provider, scope_key);

CREATE TABLE IF NOT EXISTS unsupported (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL, origin TEXT NOT NULL, category_hint TEXT, reason TEXT NOT NULL,
    raw_file_id INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
    UNIQUE (provider, origin, reason)
);

CREATE TABLE IF NOT EXISTS category_status (
    provider TEXT NOT NULL, grp TEXT NOT NULL, category TEXT,
    status TEXT NOT NULL,            -- ok | empty | denied | scope_not_granted | error
    reason TEXT, updated_at TEXT NOT NULL, job_id TEXT,
    PRIMARY KEY (provider, grp)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, provider TEXT, params TEXT NOT NULL,
    status TEXT NOT NULL, message TEXT, error TEXT, progress TEXT, warnings TEXT,
    wait_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
    owner TEXT, heartbeat REAL
);

CREATE TABLE IF NOT EXISTS job_requests (
    job_id TEXT NOT NULL, seq INTEGER NOT NULL, key TEXT NOT NULL, grp TEXT NOT NULL,
    category TEXT, scope TEXT, path TEXT NOT NULL, params TEXT NOT NULL,
    window_start TEXT, window_end TEXT,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | ok | error | skipped
    cursor TEXT, pages INTEGER NOT NULL DEFAULT 0, records INTEGER NOT NULL DEFAULT 0,
    resumed_mid_pagination INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER, reason TEXT, updated_at TEXT,
    PRIMARY KEY (job_id, key)
);
CREATE INDEX IF NOT EXISTS idx_job_requests_order ON job_requests(job_id, status, seq);

CREATE TABLE IF NOT EXISTS sync_state (
    provider TEXT PRIMARY KEY, last_synced_through TEXT, last_sync_at TEXT, last_job_id TEXT,
    last_import_start TEXT
);
"""

_FINGERPRINT_FIELDS = ("value", "value_text", "unit", "local_date", "start_local", "end_local", "start_utc",
                       "end_utc", "utc_offset_seconds", "time_basis", "source_id", "data_source", "quality",
                       "details", "scope_key", "granularity")


class Store:
    def __init__(self, path: Path, home: Path):
        self.path, self.home = Path(path), Path(home)
        private_dir(self.path.parent)
        self._init_lock = threading.Lock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
            for name, kind in (("owner", "TEXT"), ("heartbeat", "REAL")):   # databases created before 0.1.1
                if name not in columns:
                    conn.execute("ALTER TABLE jobs ADD COLUMN " + name + " " + kind)
            # 0.1.1 briefly imported Google's shared food catalog as if it were personal data. Hide those
            # rows (status 'excluded_catalog'; nothing is deleted) so queries and summaries ignore them.
            conn.execute("UPDATE records SET status='excluded_catalog' WHERE provider='google' AND "
                         "status='active' AND (source_id LIKE '%/dataTypes/food/dataPoints/%' OR "
                         "source_id LIKE '%/dataTypes/food-measurement-unit/dataPoints/%')")
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ raw files
    def save_raw(self, provider: str, origin: str, grp: str, endpoint: str, params: dict | None,
                 content: bytes, job_id: str | None, locale: str | None = None, ext: str = ".json") -> int:
        digest = sha256_bytes(content)
        relative = Path("raw") / provider / (grp or "misc") / (digest[:32] + ext)
        target = self.home / relative
        if not target.exists():
            save(target, content)
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO raw_files(provider, origin, grp, endpoint, params_json, locale, fetched_at, path, "
                "sha256, bytes, job_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (provider, origin, grp, endpoint, canonical_json(params or {}), locale, utcnow(), str(relative),
                 digest, len(content), job_id))
            return int(cur.lastrowid)

    def register_raw_path(self, provider: str, origin: str, grp: str, endpoint: str, path: Path,
                          job_id: str | None) -> int:
        data = Path(path).read_bytes()
        try:
            relative = str(Path(path).resolve().relative_to(self.home.resolve()))
        except ValueError:
            relative = str(path)
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO raw_files(provider, origin, grp, endpoint, params_json, locale, fetched_at, path, "
                "sha256, bytes, job_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (provider, origin, grp, endpoint, "{}", None, utcnow(), relative, sha256_bytes(data), len(data),
                 job_id))
            return int(cur.lastrowid)

    # -------------------------------------------------------------------- records
    def upsert(self, provider: str, parser: str, records: Iterable[Rec], raw_file_id: int | None,
               conn: sqlite3.Connection | None = None) -> dict[str, int]:
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "restored": 0}
        if conn is None:
            with self.transaction() as own:
                return self.upsert(provider, parser, records, raw_file_id, own)
        now = utcnow()
        for rec in records:
            payload_id = None
            if rec.payload is not None:
                text = canonical_json(rec.payload)
                digest = sha256_bytes(text.encode("utf-8"))
                conn.execute("INSERT OR IGNORE INTO payloads(sha256, json) VALUES (?, ?)", (digest, text))
                payload_id = conn.execute("SELECT id FROM payloads WHERE sha256 = ?", (digest,)).fetchone()[0]
            row = {name: getattr(rec, name) for name in _FINGERPRINT_FIELDS}
            row["data_source"] = canonical_json(rec.data_source) if rec.data_source is not None else None
            row["quality"] = canonical_json(sorted(set(rec.quality))) if rec.quality else None
            row["details"] = canonical_json(rec.details) if rec.details is not None else None
            fingerprint = sha256_bytes(canonical_json([row, payload_id]).encode("utf-8"))
            existing = conn.execute(
                "SELECT id, fingerprint, status FROM records WHERE provider=? AND category=? AND metric=? "
                "AND record_key=?", (provider, rec.category, rec.metric, rec.record_key)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO records(provider, category, metric, granularity, record_key, scope_key, local_date,"
                    " start_local, end_local, start_utc, end_utc, utc_offset_seconds, time_basis, value, value_text,"
                    " unit, source_id, data_source, quality, details, payload_id, raw_file_id, parser, status,"
                    " fingerprint, first_seen, last_seen, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,'active',?,?,?,?)",
                    (provider, rec.category, rec.metric, rec.granularity, rec.record_key, rec.scope_key,
                     rec.local_date, rec.start_local, rec.end_local, rec.start_utc, rec.end_utc,
                     rec.utc_offset_seconds, rec.time_basis, rec.value, rec.value_text, rec.unit, rec.source_id,
                     row["data_source"], row["quality"], row["details"], payload_id, raw_file_id, parser,
                     fingerprint, now, now, now))
                counts["inserted"] += 1
            elif existing["fingerprint"] != fingerprint or existing["status"] != "active":
                conn.execute(
                    "UPDATE records SET granularity=?, scope_key=?, local_date=?, start_local=?, end_local=?,"
                    " start_utc=?, end_utc=?, utc_offset_seconds=?, time_basis=?, value=?, value_text=?, unit=?,"
                    " source_id=?, data_source=?, quality=?, details=?, payload_id=?, raw_file_id=?, parser=?,"
                    " status='active', fingerprint=?, last_seen=?, updated_at=?, revision=revision+1 WHERE id=?",
                    (rec.granularity, rec.scope_key, rec.local_date, rec.start_local, rec.end_local, rec.start_utc,
                     rec.end_utc, rec.utc_offset_seconds, rec.time_basis, rec.value, rec.value_text, rec.unit,
                     rec.source_id, row["data_source"], row["quality"], row["details"], payload_id, raw_file_id,
                     parser, fingerprint, now, now, existing["id"]))
                counts["restored" if existing["status"] != "active" else "updated"] += 1
            else:
                conn.execute("UPDATE records SET last_seen=? WHERE id=?", (now, existing["id"]))
                counts["unchanged"] += 1
        return counts

    def reconcile_scopes(self, provider: str, seen: dict[str, set[tuple[str, str, str]]],
                         conn: sqlite3.Connection | None = None) -> int:
        """Mark records that a *complete* re-fetch of their scope no longer returns."""
        if conn is None:
            with self.transaction() as own:
                return self.reconcile_scopes(provider, seen, own)
        now, marked = utcnow(), 0
        for scope, keys in seen.items():
            rows = conn.execute("SELECT id, category, metric, record_key FROM records WHERE provider=? AND "
                                "scope_key=? AND status='active'", (provider, scope)).fetchall()
            for row in rows:
                if (row["category"], row["metric"], row["record_key"]) not in keys:
                    conn.execute("UPDATE records SET status='deleted_upstream', updated_at=?, "
                                 "revision=revision+1 WHERE id=?", (now, row["id"]))
                    marked += 1
        return marked

    def note_unsupported(self, provider: str, origin: str, hint: str | None, reason: str,
                         raw_file_id: int | None, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self.transaction() as own:
                return self.note_unsupported(provider, origin, hint, reason, raw_file_id, own)
        now = utcnow()
        conn.execute(
            "INSERT INTO unsupported(provider, origin, category_hint, reason, raw_file_id, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider, origin, reason) DO UPDATE SET last_seen=excluded.last_seen,"
            " occurrences=occurrences+1, raw_file_id=excluded.raw_file_id",
            (provider, origin[:300], hint, reason[:300], raw_file_id, now, now))

    def set_category_status(self, provider: str, grp: str, category: str | None, status: str,
                            reason: str | None, job_id: str | None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO category_status(provider, grp, category, status, reason, updated_at, job_id) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider, grp) DO UPDATE SET category=excluded.category, "
                "status=excluded.status, reason=excluded.reason, updated_at=excluded.updated_at, "
                "job_id=excluded.job_id", (provider, grp, category, status, (reason or "")[:300] or None,
                                           utcnow(), job_id))

    def delete_provider_data(self, providers: list[str]) -> dict[str, int]:
        """Local deletion of exactly these providers (only called after explicit confirmation)."""
        marks = ",".join("?" for _ in providers)
        with self.transaction() as conn:
            records = conn.execute("DELETE FROM records WHERE provider IN (" + marks + ")", providers).rowcount
            raw_rows = conn.execute("SELECT path FROM raw_files WHERE provider IN (" + marks + ")",
                                    providers).fetchall()
            for table in ("raw_files", "unsupported", "category_status", "sync_state"):
                conn.execute("DELETE FROM " + table + " WHERE provider IN (" + marks + ")", providers)
            conn.execute("DELETE FROM payloads WHERE id NOT IN (SELECT payload_id FROM records "
                         "WHERE payload_id IS NOT NULL)")
        removed = 0
        home = self.home.resolve()
        for row in raw_rows:
            target = (self.home / row["path"]).resolve()
            try:
                target.relative_to(home)  # never delete outside the data home
            except ValueError:
                continue
            if target.is_file():
                target.unlink()
                removed += 1
        return {"records_deleted": records, "raw_files_deleted": removed}


# ------------------------------------------------------------------ cursor helpers

def encode_cursor(values: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(values, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception:
        raise ValueError("cursor is invalid; pass the next_cursor value from a previous result") from None
