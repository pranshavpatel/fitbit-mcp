"""Persistent background jobs. MCP tool calls only enqueue work and read state, so they
never block on OAuth, imports or rate-limit waits.

Several server processes may share one database (for example Claude Code and the Claude
desktop app each start one, and a client may start two at once). So:

* every job records the process that owns it and a heartbeat that only that process's
  live worker refreshes;
* a job whose owner stopped heart-beating is detected by any other running server (or the
  next one to start), marked interrupted, and - unless FITBIT_MCP_AUTO_RESUME=0 - resumed
  there; resuming skips every request already completed;
* claiming a job is an atomic status transition, so two processes never run the same job;
* cancellation is stored in the database and polled by the worker, so cancel_job works no
  matter which process receives the call.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .client import NetworkError, RateLimitExhausted
from .oauth import AuthError, AuthRequired, Cancelled
from .store import Store
from .util import log, redact, utcnow

ACTIVE = ("queued", "running", "waiting_rate_limit")
FINAL = ("succeeded", "partial", "failed", "cancelled", "interrupted")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, manager: "JobManager", job_id: str, params: dict, provider: str | None):
        self.manager, self.id, self.params, self.provider = manager, job_id, params, provider
        self.store = manager.store
        self.cancel_event = manager.cancel_event(job_id)
        self._progress: dict[str, Any] = {}
        self._last_flush = 0.0
        self._last_cancel_check = 0.0
        job = manager.get(job_id)
        if job and job.get("progress"):
            self._progress = dict(job["progress"])

    def _cancel_requested(self) -> bool:
        if self.cancel_event.is_set():
            return True
        now = time.monotonic()
        if now - self._last_cancel_check >= 1.0:      # cancel may have been requested via another process
            self._last_cancel_check = now
            with self.store.connect() as conn:
                row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (self.id,)).fetchone()
            if row and row["cancel_requested"]:
                self.cancel_event.set()
                return True
        return False

    def checkpoint(self) -> None:
        if self._cancel_requested():
            raise JobCancelled()

    def wait(self, seconds: float, reason: str) -> None:
        """Cancellable wait used for rate limits and back-off."""
        until = datetime.fromtimestamp(time.time() + seconds, timezone.utc).replace(microsecond=0)
        self.manager._update(self.id, status="waiting_rate_limit" if "rate" in reason else "running",
                             wait_until=until.isoformat().replace("+00:00", "Z"),
                             message="Waiting {}s ({})".format(int(seconds), reason))
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self.cancel_event.wait(min(remaining, 1.0)) or self._cancel_requested():
                raise JobCancelled()
        self.manager._update(self.id, status="running", wait_until=None, message="Resumed after " + reason)

    def progress(self, force: bool = False, **values: Any) -> None:
        self._progress.update(values)
        now = time.monotonic()
        if force or now - self._last_flush > 1.0:
            self._last_flush = now
            self.manager._update(self.id, progress=json.dumps(self._progress))

    def message(self, text: str) -> None:
        self.manager._update(self.id, message=text[:500])

    def warn(self, text: str) -> None:
        job = self.manager.get(self.id) or {}
        warnings = list(job.get("warnings") or [])
        if text not in warnings and len(warnings) < 200:
            warnings.append(text[:300])
            self.manager._update(self.id, warnings=json.dumps(warnings))


Runner = Callable[[JobContext], dict]


class JobManager:
    def __init__(self, store: Store, runners: dict[str, Runner]):
        self.store, self.runners = store, runners
        self.owner = "{}:{}:{}".format(socket.gethostname(), os.getpid(), uuid.uuid4().hex[:8])
        self.heartbeat_seconds = _env_float("FITBIT_MCP_JOB_HEARTBEAT_SECONDS", 15.0)
        self.stale_seconds = _env_float("FITBIT_MCP_JOB_STALE_SECONDS", 90.0)
        self.auto_resume = os.environ.get("FITBIT_MCP_AUTO_RESUME", "1") != "0"
        self._lanes: dict[str, queue.Queue] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._owned: set[str] = set()      # queued here or executing in a live worker of this process
        self._lock = threading.Lock()
        self._done = threading.Condition()
        self._stop = threading.Event()
        self._heart: threading.Thread | None = None

    # ------------------------------------------------------------------ liveness
    def start(self) -> None:
        """Start the heartbeat / takeover thread (idempotent)."""
        with self._lock:
            if self._heart is None or not self._heart.is_alive():
                self._heart = threading.Thread(target=self._heartbeat_loop, name="job-heartbeat", daemon=True)
                self._heart.start()

    def stop(self) -> None:
        self._stop.set()

    def _beat(self) -> None:
        with self._lock:
            owned = list(self._owned)
        if not owned:
            return
        marks = ",".join("?" for _ in owned)
        with self.store.transaction() as conn:
            conn.execute("UPDATE jobs SET heartbeat=? WHERE owner=? AND id IN (" + marks + ") AND status IN "
                         "('queued','running','waiting_rate_limit')", [time.time(), self.owner] + owned)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self._beat()
                self.recover(startup=False)
            except Exception:  # pragma: no cover - never let liveness tracking kill the server
                log.exception("job heartbeat failed")

    def recover(self, startup: bool = True) -> list[str]:
        """Interrupt jobs whose owning process stopped heart-beating; optionally resume them here.

        Jobs owned by a live process (fresh heartbeat) are left alone, so starting a second
        server never disturbs an import another server is running.
        """
        cutoff = time.time() - self.stale_seconds
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT id, kind, heartbeat FROM jobs WHERE status IN ('queued','running','waiting_rate_limit') "
                "AND (owner IS NULL OR owner != ?) AND (heartbeat IS NULL OR heartbeat < ?)",
                (self.owner, cutoff)).fetchall()
        interrupted = []
        for row in rows:
            with self.store.transaction() as conn:
                changed = conn.execute(
                    "UPDATE jobs SET status='interrupted', wait_until=NULL, updated_at=?, message=? WHERE id=? AND "
                    "status IN ('queued','running','waiting_rate_limit') AND (owner IS NULL OR owner != ?) AND "
                    "(heartbeat IS NULL OR heartbeat < ?)",
                    (utcnow(), "The server process running this job stopped; completed requests are kept.",
                     row["id"], self.owner, cutoff)).rowcount
            if changed:
                interrupted.append(row["id"])
                log.info("Job %s lost its server process; marked interrupted", row["id"])
                if self.auto_resume and row["kind"] in ("import", "sync", "takeout"):
                    try:
                        self.resume(row["id"])
                        log.info("Job %s resumed by %s", row["id"], self.owner)
                    except (ValueError, KeyError):
                        pass
        return interrupted

    # ---------------------------------------------------------------- persistence
    def _update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow()
        allowed = {"status", "message", "error", "progress", "warnings", "wait_until", "updated_at", "started_at",
                   "finished_at", "cancel_requested", "params", "owner", "heartbeat"}
        names = [k for k in fields if k in allowed]
        assignments = ", ".join("{}=?".format(n) for n in names)  # column names come from the allowlist above
        with self.store.transaction() as conn:
            conn.execute("UPDATE jobs SET " + assignments + " WHERE id=?", [fields[n] for n in names] + [job_id])

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        for key in ("params", "progress", "warnings"):
            job[key] = json.loads(job[key]) if job.get(key) else ({} if key != "warnings" else [])
        job["cancel_requested"] = bool(job["cancel_requested"])
        job["stalled"] = bool(job["status"] in ACTIVE and job.get("owner") != self.owner and
                              (job.get("heartbeat") or 0) < time.time() - self.stale_seconds)
        return job

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]
        return [self.get(i) for i in ids]

    def active(self, kind: str | None = None, provider: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT id FROM jobs WHERE status IN ('queued','running','waiting_rate_limit')", []
        if kind:
            sql, args = sql + " AND kind=?", args + [kind]
        if provider:
            sql, args = sql + " AND provider=?", args + [provider]
        with self.store.connect() as conn:
            ids = [r["id"] for r in conn.execute(sql, args)]
        return [j for j in (self.get(i) for i in ids) if j and not j["stalled"]]

    # -------------------------------------------------------------------- control
    def cancel_event(self, job_id: str) -> threading.Event:
        with self._lock:
            return self._cancel.setdefault(job_id, threading.Event())

    def submit(self, kind: str, provider: str | None, params: dict, lane: str) -> dict[str, Any]:
        if kind not in self.runners:
            raise ValueError("unknown job kind " + kind)
        job_id = "job_" + uuid.uuid4().hex[:12]
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO jobs(id, kind, provider, params, status, message, created_at, updated_at, "
                         "progress, warnings, owner, heartbeat) VALUES (?,?,?,?, 'queued', 'Queued', ?, ?, '{}', "
                         "'[]', ?, ?)", (job_id, kind, provider, json.dumps(dict(params, _lane=lane)), now, now,
                                         self.owner, time.time()))
        self._enqueue(job_id, lane)
        return self.get(job_id)

    def _enqueue(self, job_id: str, lane: str) -> None:
        self.start()
        with self._lock:
            self._owned.add(job_id)
            q = self._lanes.setdefault(lane, queue.Queue())
            thread = self._threads.get(lane)
            if thread is None or not thread.is_alive():
                thread = threading.Thread(target=self._worker, args=(lane, q), name="job-" + lane, daemon=True)
                self._threads[lane] = thread
                thread.start()
        q.put(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] in FINAL:
            return job
        self.cancel_event(job_id).set()
        with self.store.transaction() as conn:
            queued = conn.execute("UPDATE jobs SET status='cancelled', cancel_requested=1, finished_at=?, "
                                  "updated_at=?, message='Cancelled before it started' WHERE id=? AND "
                                  "status='queued'", (utcnow(), utcnow(), job_id)).rowcount
            if not queued and job["stalled"]:
                conn.execute("UPDATE jobs SET status='cancelled', cancel_requested=1, finished_at=?, updated_at=?, "
                             "message='Cancelled (its server process had stopped)' WHERE id=?",
                             (utcnow(), utcnow(), job_id))
            elif not queued:
                conn.execute("UPDATE jobs SET cancel_requested=1, updated_at=?, message='Cancellation requested; "
                             "stopping at the next safe point' WHERE id=?", (utcnow(), job_id))
        return self.get(job_id)

    def resume(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] in ACTIVE and not job["stalled"]:
            return job
        if job["status"] == "succeeded":
            raise ValueError("Job already succeeded; use sync_data for newer data.")
        if job["kind"] == "connect":
            raise ValueError("Sign-in jobs cannot be resumed; call connect_account again.")
        with self._lock:
            self._cancel[job_id] = threading.Event()
        with self.store.transaction() as conn:
            claimed = conn.execute(
                "UPDATE jobs SET status='queued', message='Queued to resume', error=NULL, cancel_requested=0, "
                "finished_at=NULL, wait_until=NULL, owner=?, heartbeat=?, updated_at=? WHERE id=? AND status=?",
                (self.owner, time.time(), utcnow(), job_id, job["status"])).rowcount
            if not claimed:   # another process resumed it first
                return self.get(job_id)
            # retry failed requests too; completed ones are skipped by the runner
            conn.execute("UPDATE job_requests SET status='pending' WHERE job_id=? AND status='error'", (job_id,))
        self._enqueue(job_id, job["params"].get("_lane", "default"))
        return self.get(job_id)

    def wait_for(self, job_id: str, timeout: float = 30.0, statuses: tuple[str, ...] = FINAL) -> dict[str, Any]:
        """Test helper: block until the job reaches one of `statuses`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if job and job["status"] in statuses:
                return job
            with self._done:
                self._done.wait(0.05)
        raise TimeoutError("job {} did not reach {} (last: {})".format(job_id, statuses, self.get(job_id)))

    # --------------------------------------------------------------------- worker
    def _worker(self, lane: str, q: queue.Queue) -> None:
        while True:
            job_id = q.get()
            try:
                self._run(job_id)
            except Exception:  # pragma: no cover - last-resort guard
                log.exception("job worker crashed on %s", job_id)
            finally:
                with self._lock:
                    self._owned.discard(job_id)
                with self._done:
                    self._done.notify_all()

    def _run(self, job_id: str) -> None:
        try:
            self._run_claimed(job_id)
        finally:
            with self._lock:   # also on BaseException: a dead worker must stop heart-beating
                self._owned.discard(job_id)

    def _run_claimed(self, job_id: str) -> None:
        with self.store.transaction() as conn:
            claimed = conn.execute(
                "UPDATE jobs SET status='running', owner=?, heartbeat=?, started_at=COALESCE(started_at, ?), "
                "message='Running', updated_at=? WHERE id=? AND status='queued' AND cancel_requested=0",
                (self.owner, time.time(), utcnow(), utcnow(), job_id)).rowcount
        if not claimed:
            job = self.get(job_id)
            if job and job["status"] == "queued" and job["cancel_requested"]:
                self._update(job_id, status="cancelled", finished_at=utcnow(), message="Cancelled")
            return
        job = self.get(job_id)
        context = JobContext(self, job_id, job["params"], job["provider"])
        try:
            result = self.runners[job["kind"]](context) or {}
            status = result.pop("status", "succeeded")
            context.progress(force=True, **result)
            self._update(job_id, status=status, finished_at=utcnow(), wait_until=None,
                         message=result.get("message") or ("Completed" if status == "succeeded" else
                                                           "Completed with missing categories"))
        except (JobCancelled, Cancelled):
            self._update(job_id, status="cancelled", finished_at=utcnow(), wait_until=None,
                         message="Cancelled; completed requests are kept. resume_job continues from here.")
        except AuthRequired as error:
            self._update(job_id, status="failed", finished_at=utcnow(), wait_until=None, error=redact(error),
                         message="Authorization needed: run connect_account, then resume_job.")
        except RateLimitExhausted as error:
            self._update(job_id, status="interrupted", finished_at=utcnow(), wait_until=None,
                         error=redact(error), message="Stopped at a long rate-limit window; resume_job later.")
        except NetworkError as error:
            self._update(job_id, status="interrupted", finished_at=utcnow(), wait_until=None,
                         error=redact(error), message="Network problem; resume_job when online.")
        except AuthError as error:
            self._update(job_id, status="failed", finished_at=utcnow(), wait_until=None, error=redact(error),
                         message=redact(error))
        except Exception as error:  # report a sanitized summary, keep the traceback in stderr only
            log.exception("job %s failed", job_id)
            self._update(job_id, status="failed", finished_at=utcnow(), wait_until=None,
                         error=redact("{}: {}".format(type(error).__name__, error))[:500],
                         message="Failed; see error. Completed work is kept and resume_job can retry.")
