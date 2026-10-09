"""The phone app's server. Synthetic data only; runs a real server on a random local port."""
import json
import threading
import urllib.error
import urllib.request

import pytest

import appserver as A
import fixture_db
import journal as J
import lifts as L


@pytest.fixture
def srv(tmp_path, monkeypatch):
    fixture_db.create(tmp_path / "fitbit.sqlite3")
    (tmp_path / "coaching").mkdir(exist_ok=True)
    (tmp_path / "coaching" / "stats.json").write_text(json.dumps(fixture_db.CONFIG))
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    server = A.make_server(tmp_path, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:{}".format(server.server_address[1]), tmp_path, server
    server.shutdown()
    server.server_close()


def call(base, path, body=None, headers=None, method=None):
    h = {"Content-Type": "application/json", "X-Fitdash": "1"} if body is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw.startswith(b"{") else raw), e.headers


def test_binds_localhost_only(srv):
    assert srv[2].server_address[0] == "127.0.0.1"


def test_serves_the_app_and_blocks_traversal(srv):
    base = srv[0]
    code, body, h = call(base, "/")
    assert code == 200 and b'<nav class="tabs"' in body and "default-src 'self'" in h["Content-Security-Policy"]
    for f in ("/app.js", "/app.css", "/manifest.webmanifest", "/icon.svg", "/icon-180.png"):
        assert call(base, f)[0] == 200, f
    assert call(base, "/../scripts/data.py")[0] == 404
    assert call(base, "/%2e%2e/scripts/data.py")[0] == 404
    assert call(base, "/terminal")[0] == 404                       # not generated in this home


def test_model(srv):
    code, m, _ = call(srv[0], "/api/model")
    assert code == 200 and m["has_data"] and m["date"] == m["days"][-1]
    assert "attention" in m and m["habits"] and "category_status" not in m
    assert all(len(p) == 2 for p in m["heart"]["hr_today"])


def test_writes_need_the_header_and_json(srv):
    base = srv[0]
    assert call(base, "/api/journal", {"habits": {}}, headers={"X-Fitdash": ""})[0] == 403
    assert call(base, "/api/journal", {"habits": {}}, headers={"Content-Type": "text/plain"})[0] == 403
    assert call(base, "/api/journal", method="OPTIONS")[0] == 405


def test_journal_roundtrip(srv):
    base, home, _ = srv
    code, v, _ = call(base, "/api/journal", {"date": "today", "habits": {"alcohol": False, "stretch": True}, "note": " ok "})
    assert code == 200 and v["journal"]["habits"] == {"alcohol": False, "stretch": True} and v["journal"]["note"] == "ok"
    day = v["date"]
    assert J.load(home)[day]["habits"]["stretch"] is True
    code, v, _ = call(base, "/api/journal", {"date": "today", "habits": {"stretch": None}})
    assert "stretch" not in v["journal"]["habits"]                  # null clears a habit
    assert call(base, "/api/journal", {"habits": {"nope": True}})[0] == 400
    assert call(base, "/api/journal", {"date": "2999-01-01", "habits": {}})[0] == 400
    code, v, _ = call(base, "/api/log?date=today")
    assert code == 200 and v["journal"]["note"] == "ok" and v["habits"]


def test_lift_roundtrip(srv):
    base, home, _ = srv
    code, v, _ = call(base, "/api/lift", {"date": "yesterday", "text": "bench 3x8@60, row 4x10@50"})
    assert code == 200 and v["added"] == ["bench press 3x8@60", "row 4x10@50"]
    assert v["sets"]["chest"] == 3 and len(L.load(home)[v["date"]]) == 2
    code, err, _ = call(base, "/api/lift", {"date": "yesterday", "text": "zumba 3x8"})
    assert code == 400 and "don't know" in err["error"]
    code, v, _ = call(base, "/api/lift/undo", {"date": "yesterday"})
    assert code == 200 and v["removed"] == "row 4x10@50" and len(v["lifts"]) == 1
    assert call(base, "/api/lift/undo", {"date": "today"})[0] == 400


def test_model_refreshes_after_a_write(srv):
    base = srv[0]
    call(base, "/api/model")
    call(base, "/api/lift", {"date": "today", "text": "squat 5x5@100"})
    _, m, _ = call(base, "/api/model")
    assert m["lifting"]["has_log"]


def test_sync_unavailable_without_a_command(srv):
    base = srv[0]
    assert call(base, "/api/sync")[1]["available"] is False
    assert call(base, "/api/sync", {})[0] == 400
