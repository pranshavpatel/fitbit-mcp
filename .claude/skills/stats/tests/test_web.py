"""The phone page. Synthetic data only."""
import plistlib
import re

import dashboard
import fixture_db
import web as W
from test_render import _model


def test_html_page_is_self_contained(tmp_path, monkeypatch):
    m = _model(tmp_path, monkeypatch)
    page = W.to_html(lambda con: dashboard.render(m, con, W.PHONE_WIDTH), updated="Thu 8 Oct, 9:00 AM")
    assert page.startswith("<!doctype html>") and 'name="viewport"' in page
    assert "Today" in page and "Recovery" in page and "Updated Thu 8 Oct, 9:00 AM" in page
    assert "<script" not in page.lower()
    assert not re.search(r"(src|href)=[\"']?https?://", page)              # nothing loaded from the internet
    assert "#111111" in page and "color: #" in page                        # dark page, inline colors


def test_cli_html_writes_file(tmp_path, monkeypatch, capsys):
    fixture_db.create(tmp_path / "fitbit.sqlite3")
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    import subprocess
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    assert dashboard.main(["--html", "--no-sync"]) == 0
    out = tmp_path / "web" / "index.html"
    assert out.exists() and "Muscle Freshness" in out.read_text()
    assert not calls                                                       # --no-sync: no sync
    assert dashboard.main(["--html", str(tmp_path / "x.html"), "--no-sync", "--section", "sleep"]) == 0
    assert "Sleep" in (tmp_path / "x.html").read_text()


def test_install_binds_localhost_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(W.Path, "home", classmethod(lambda cls: tmp_path))
    calls = []

    class R:
        returncode, stderr, stdout = 0, "", ""

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda args, **k: calls.append(args) or R())
    assert W.install(tmp_path / ".fitbit-mcp", every_min=30) == 0
    server = plistlib.loads(W._plist(W.LABELS["server"]).read_bytes())
    args = server["ProgramArguments"]
    assert args[-3:] == ["app", "--port", str(W.PORT)]                      # appserver binds 127.0.0.1 itself
    refresh = plistlib.loads(W._plist(W.LABELS["refresh"]).read_bytes())
    assert refresh["StartInterval"] == 1800 and "--html" in refresh["ProgramArguments"]
    assert "tailscale serve --bg 8787" in capsys.readouterr().out
    assert W.uninstall() == 0
    assert not W._plist(W.LABELS["server"]).exists() and not W._plist(W.LABELS["refresh"]).exists()


def test_pin_cells_fixes_fallback_glyph_widths():
    body = '<pre><span style="color: #fff">⣀⠉ ● ╭─█ ok</span></pre>'
    out = W.pin_cells(body)
    assert out.count('<span class="c">') == 3                                # two braille + ●
    assert "╭─█ ok" in out and '<span style="color: #fff">' in out           # box/blocks/ASCII and tags untouched


def test_page_pins_every_ring_glyph(tmp_path, monkeypatch):
    m = _model(tmp_path, monkeypatch)
    page = W.to_html(lambda con: dashboard.render(m, con, W.PHONE_WIDTH))
    page = page[page.index("<pre"):]
    text_only = re.sub(r"<[^>]+>", "", re.sub(r'<span class="c">.</span>', "", page))
    assert not re.search(r"[⠀-⣿●○◐★✓✕▲▶]", text_only)               # none left unpinned
