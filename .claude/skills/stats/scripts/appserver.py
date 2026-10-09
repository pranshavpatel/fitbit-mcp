"""The fitdash phone app's server: the app itself, its data as JSON, and logging from the phone.

    fitdash app [--port 8787]          serve on 127.0.0.1 (reach it from your phone with Tailscale)

Routes
    GET  /                    the app (../app/index.html and its files)
    GET  /terminal            the terminal-style page (~/.fitbit-mcp/web/index.html), if generated
    GET  /api/model           the dashboard model (the same numbers as `fitdash --json`), slimmed
    GET  /api/log?date=       that day's journal entry and lift log, the habit list, known exercises
    POST /api/journal         {"date", "habits": {key: true|false|null}, "note"}
    POST /api/lift            {"date", "text": "bench 3x8@60 row 4x10@50"} → parsed entries or an error
    POST /api/lift/undo       {"date"}
    POST /api/priorities      {"date", "items": ["…", "…", "…"]} | {"date", "index": 1-3, "status": "done"|"partial"|"missed"|null}
                              | {"date", "reflection": "…"}
    POST /api/sync            start a Google sync in the background
    GET  /api/sync            {"running", "last"}

It listens on 127.0.0.1 only. Writes need `Content-Type: application/json` and an
`X-Fitdash: 1` header, which a browser can't send cross-site without a CORS preflight that this
server never approves, so another website open on the phone can't log anything. Everything it
writes is your own logs in ~/.fitbit-mcp; the Fitbit database is only ever read.
"""
from __future__ import annotations

import json
import mimetypes
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import data as D
import journal as J
import lifts as L
import priorities as PR
import scores as S

APP_DIR = Path(__file__).resolve().parent.parent / "app"
MAX_BODY = 64 * 1024
MODEL_TTL_S = 20                         # rebuild the model at most this often (a build takes ~1 s)


class State:
    def __init__(self, home: Path, sync_cmd: list[str] | None):
        self.home = home
        self.sync_cmd = sync_cmd
        self.lock = threading.Lock()
        self.model: dict | None = None
        self.model_at = 0.0
        self.sync_proc: subprocess.Popen | None = None
        self.sync_last: dict | None = None

    def invalidate(self) -> None:
        with self.lock:
            self.model, self.model_at = None, 0.0


def slim(m: dict) -> dict:
    """Drop what the app never draws (raw category lists) and thin today's HR to 5-minute points."""
    m = dict(m)
    for k in ("category_status", "metrics", "devices", "coverage"):
        m.pop(k, None)
    h = dict(m.get("heart") or {})
    pts = h.get("hr_today") or []
    if pts:
        buckets: dict[int, list[float]] = {}
        for t, v in pts:
            if t is not None and v is not None:
                buckets.setdefault(int(t // 5), []).append(v)
        h["hr_today"] = [[k * 5, round(sum(v) / len(v))] for k, v in sorted(buckets.items())]
    m["heart"] = h
    return m


def build(state: State) -> dict:
    with state.lock:
        if state.model is not None and time.monotonic() - state.model_at < MODEL_TTL_S:
            return state.model
    now = datetime.now(D.NY)
    cfg = D.load_config(state.home)
    store = D.open_store(state.home)
    try:
        m = D.build_model(store, now.date(), cfg, now=now)
    finally:
        if store:
            store.close()
    m = slim(m)
    m["habits"] = J.habits(cfg)
    m["sync"] = sync_status(state)
    with state.lock:
        state.model, state.model_at = m, time.monotonic()
    return m


def sync_status(state: State) -> dict:
    p = state.sync_proc
    if p is not None and p.poll() is not None:
        state.sync_last = {"ok": p.returncode == 0, "at": datetime.now(D.NY).isoformat(timespec="minutes")}
        state.sync_proc = None
        state.invalidate()
    return {"running": state.sync_proc is not None, "last": state.sync_last, "available": bool(state.sync_cmd)}


def day_arg(s: str | None) -> date:
    today = datetime.now(D.NY).date()
    if not s or s == "today":
        return today
    if s == "yesterday":
        return today - timedelta(days=1)
    d = date.fromisoformat(s)
    if d > today:
        raise ValueError("can't log a future day")
    return d


def log_view(state: State, d: date) -> dict:
    cfg = D.load_config(state.home)
    hs = J.habits(cfg)
    entry = J.load(state.home).get(d.isoformat()) or {"habits": {}, "note": ""}
    lifts = L.load(state.home).get(d.isoformat()) or []
    recent = []
    for day_ in sorted(L.load(state.home), reverse=True)[:20]:
        for e in L.load(state.home)[day_]:
            if e["exercise"] not in recent:
                recent.append(e["exercise"])
    pd = _plan_day(d)
    return {"date": d.isoformat(), "habits": hs, "journal": entry,
            "priorities": {"date": pd.isoformat(), **(PR.load(state.home).get(pd.isoformat()) or {"items": [], "reflection": ""})},
            "lifts": [{**e, "text": L.describe(e)} for e in lifts],
            "sets": {k: v for k, v in S.weekly_sets(lifts).items() if v},
            "recent_exercises": recent[:12], "exercises": sorted(S.EXERCISES)}


def _plan_day(d: date) -> date:
    """Today's priorities after midnight are still last night's list (priorities.plan_day)."""
    now = datetime.now(D.NY)
    return PR.plan_day(now) if d == now.date() else d


def save_priorities(state: State, body: dict) -> dict:
    d = day_arg(body.get("date"))
    pd, now = _plan_day(d), datetime.now(D.NY)
    if "items" in body:
        items = body["items"]
        if not isinstance(items, list) or not all(isinstance(t, str) for t in items):
            raise ValueError("items must be a list of text")
        PR.set_items(state.home, pd, items, now)
    if "index" in body:
        st = body.get("status")
        if st not in (None, "done", "partial", "missed"):
            raise ValueError("status is done, partial, missed or null")
        PR.mark(state.home, pd, int(body["index"]), st, now)
    if "reflection" in body:
        if not isinstance(body["reflection"], str):
            raise ValueError("reflection must be text")
        PR.reflect(state.home, pd, body["reflection"], now)
    state.invalidate()
    return log_view(state, d)


def save_journal(state: State, body: dict) -> dict:
    d = day_arg(body.get("date"))
    hs = {h["key"] for h in J.habits(D.load_config(state.home))}
    marks = body.get("habits") or {}
    if not isinstance(marks, dict) or any(k not in hs for k in marks):
        raise ValueError("unknown habit")
    days = J.load(state.home)
    entry = days.get(d.isoformat()) or {"habits": {}, "note": ""}
    for k, v in marks.items():
        if v is None:
            entry["habits"].pop(k, None)
        elif isinstance(v, bool):
            entry["habits"][k] = v
        else:
            raise ValueError("habit values are true, false or null")
    if "note" in body:
        note = body.get("note") or ""
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("note must be text under 2000 characters")
        entry["note"] = note.strip()
    if entry["habits"] or entry["note"]:
        days[d.isoformat()] = entry
    else:
        days.pop(d.isoformat(), None)
    J.save(state.home, days)
    state.invalidate()
    return log_view(state, d)


def save_lift(state: State, body: dict) -> dict:
    d = day_arg(body.get("date"))
    text = body.get("text") or ""
    if not isinstance(text, str) or len(text) > 1000:
        raise ValueError("text too long")
    now = datetime.now(D.NY)
    at = now.strftime("%Y-%m-%dT%H:%M") if d == now.date() else d.isoformat() + "T18:00"
    new = L.parse(text.replace(",", " ").split(), at)        # raises LiftError with a readable message
    days = L.load(state.home)
    days.setdefault(d.isoformat(), []).extend(new)
    L.save(state.home, days)
    state.invalidate()
    return {**log_view(state, d), "added": [L.describe(e) for e in new]}


def undo_lift(state: State, body: dict) -> dict:
    d = day_arg(body.get("date"))
    days = L.load(state.home)
    if not days.get(d.isoformat()):
        raise ValueError("nothing logged that day")
    gone = days[d.isoformat()].pop()
    if not days[d.isoformat()]:
        del days[d.isoformat()]
    L.save(state.home, days)
    state.invalidate()
    return {**log_view(state, d), "removed": L.describe(gone)}


def start_sync(state: State) -> dict:
    if not state.sync_cmd:
        raise ValueError("sync isn't available here")
    if state.sync_proc is None or state.sync_proc.poll() is not None:
        state.sync_proc = subprocess.Popen(state.sync_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return sync_status(state)


class Handler(BaseHTTPRequestHandler):
    state: State
    server_version = "fitdash"

    def log_message(self, fmt, *args):            # quiet: one line per request to stderr
        sys.stderr.write("{} {}\n".format(datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def _error(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        try:
            if path == "/api/model":
                return self._json(build(self.state))
            if path == "/api/log":
                q = parse_qs(url.query)
                return self._json(log_view(self.state, day_arg((q.get("date") or [None])[0])))
            if path == "/api/sync":
                return self._json(sync_status(self.state))
        except ValueError as exc:
            return self._error(400, str(exc))
        if path == "/terminal":
            page = self.state.home / "web" / "index.html"
            if page.exists():
                return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            return self._error(404, "the terminal page hasn't been generated (fitdash --html)")
        if path in ("/", "/index.html"):
            path = "/index.html"
        f = (APP_DIR / path.lstrip("/")).resolve()
        if APP_DIR.resolve() not in f.parents or not f.is_file():
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.suffix == ".webmanifest":
            ctype = "application/manifest+json"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype, "no-cache")

    def do_POST(self):
        path = urlparse(self.path).path
        if self.headers.get("X-Fitdash") != "1" or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(403, "missing X-Fitdash header or JSON content type")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._error(413, "too large")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            if path == "/api/journal":
                return self._json(save_journal(self.state, body))
            if path == "/api/lift":
                return self._json(save_lift(self.state, body))
            if path == "/api/lift/undo":
                return self._json(undo_lift(self.state, body))
            if path == "/api/priorities":
                return self._json(save_priorities(self.state, body))
            if path == "/api/sync":
                return self._json(start_sync(self.state))
        except (ValueError, TypeError, L.LiftError, PR.PriorityError) as exc:
            return self._error(400, str(exc))
        return self._error(404, "not found")

    def do_OPTIONS(self):                           # no CORS: cross-site writes stay blocked
        self._error(405, "not allowed")


def make_server(home: Path, port: int = 8787, host: str = "127.0.0.1",
                sync_cmd: list[str] | None = None) -> ThreadingHTTPServer:
    handler = type("FitdashHandler", (Handler,), {"state": State(home, sync_cmd)})
    return ThreadingHTTPServer((host, port), handler)


def serve(home: Path, port: int, sync_cmd: list[str] | None) -> int:
    srv = make_server(home, port, sync_cmd=sync_cmd)
    print("fitdash app on http://127.0.0.1:{} (Tailscale: tailscale serve --bg {})".format(port, port), file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
