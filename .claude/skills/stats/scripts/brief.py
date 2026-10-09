"""The morning brief: sync, then one macOS notification with today's call.

    fitdash brief                 sync, then notify (and print the same text)
    fitdash brief --print         sync and print only, no notification
    fitdash brief --install       run it every morning (default: 30 min after your wake anchor)
    fitdash brief --install 09:00
    fitdash brief --uninstall
    fitdash brief --status

Scheduling uses a per-user launchd agent (~/Library/LaunchAgents/com.fitdash.brief.plist): it runs
on this Mac only, and a Mac that was asleep at that time runs it when it wakes. Output is appended
to ~/.fitbit-mcp/brief.log. The sync is the usual read-only Google sync; nothing else goes out.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import data as D

LABEL = "com.fitdash.brief"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def _clock(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    m = int(round(minutes)) % 1440
    return "{}:{:02d}".format(m // 60, m % 60)


def compose(m: dict) -> tuple[str, str]:
    """(title, body) for the notification. Short: macOS shows about two lines of the body."""
    if not m.get("has_data"):
        return "fitdash", "No data yet: {}".format(m.get("empty_reason", "empty"))
    rec = m["recovery"]
    sc = ((m.get("sleep") or {}).get("score") or {}).get("score")
    title_parts = []
    title_parts.append("Recovery {}%".format(rec["score"]) if rec["score"] is not None else "Recovery —")
    if sc is not None:
        title_parts.append("Sleep {}".format(sc))
    asleep = (m.get("sleep") or {}).get("asleep")
    if asleep:
        title_parts.append("{}h{:02d}".format(int(asleep // 60), int(asleep % 60)))
    body = []
    band = rec.get("band")
    target = m["strain"].get("target")
    if band == "green":
        body.append("Go hard: strain {:.0f}-{:.0f}.".format(*target))
    elif band == "yellow":
        body.append("Moderate day: strain {:.0f}-{:.0f}.".format(*target))
    elif band == "red":
        body.append("Recover: keep strain under {:.0f}.".format(target[1]))
    sp = m["training"]["split"]
    nxt = sp.get("next")
    if nxt:
        fresh = (sp.get("fresh") or {}).get(nxt)
        body.append("Next: {}{}.".format(nxt, "" if fresh is None else " ({}% fresh)".format(fresh)))
    if m["training"].get("impact_spike_recent"):
        body.append("Knee: no runs/jumps.")
    tn = (m.get("sleep") or {}).get("tonight") or {}
    if tn.get("asleep_by") is not None:
        body.append("Asleep by {} tonight.".format(_clock(tn["asleep_by"])))
    lf = m.get("lifting") or {}
    if lf.get("has_log") and lf.get("under"):
        body.append("Low sets: {}.".format(", ".join(lf["under"][:3])))
    if not ((m.get("priorities") or {}).get("today") or {}).get("items"):
        body.append("Set today's top 3: fitdash priorities.")
    j = m.get("journal") or {}
    if j.get("has_log") and not j.get("yesterday_logged") and not j.get("today_logged"):
        body.append("Journal yesterday: fitdash journal.")
    stale = _stale_hours(m.get("last_sync"))
    if stale is not None and stale > 6:
        title_parts.append("data {:.0f}h old".format(stale))
    return " · ".join(title_parts), " ".join(body) or m["verdict"]["headline"]


def _stale_hours(last_sync: str | None) -> float | None:
    if not last_sync:
        return None
    try:
        t = datetime.fromisoformat(last_sync)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=D.NY)
    return (datetime.now(D.NY) - t).total_seconds() / 3600


def _osa_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, body: str) -> bool:
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    script = "display notification {} with title {} sound name \"default\"".format(_osa_quote(body), _osa_quote(title))
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    return r.returncode == 0


def default_time(cfg: dict) -> str:
    wake = D.hhmm(cfg.get("wake_anchor")) if cfg.get("wake_anchor") else None
    return cfg.get("brief_time") or ("09:00" if wake is None else _clock(wake + 30))


def launcher() -> list[str]:
    """How launchd should start `fitdash brief`: the fitdash wrapper if installed, else this script via uv."""
    wrapper = shutil.which("fitdash") or str(Path.home() / ".local" / "bin" / "fitdash")
    if Path(wrapper).exists():
        return [wrapper, "brief"]
    uv = shutil.which("uv") or "uv"
    return [uv, "run", "--quiet", "--script", str(Path(__file__).with_name("dashboard.py")), "brief"]


def install(at: str, home: Path) -> int:
    try:
        hh, mm = (int(x) for x in at.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except (ValueError, AssertionError):
        print("time must be HH:MM, e.g. 09:00", file=sys.stderr)
        return 2
    uv_dir = str(Path(shutil.which("uv") or "/opt/homebrew/bin/uv").parent)
    path_env = ":".join(dict.fromkeys([str(Path.home() / ".local" / "bin"), uv_dir, "/opt/homebrew/bin",
                                       "/usr/local/bin", "/usr/bin", "/bin"]))
    home.mkdir(parents=True, exist_ok=True)
    log = str(home / "brief.log")
    plist = {
        "Label": LABEL,
        "ProgramArguments": launcher(),
        "StartCalendarInterval": {"Hour": hh, "Minute": mm},
        "EnvironmentVariables": {"PATH": path_env},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "RunAtLoad": False,
    }
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    domain = "gui/{}".format(os.getuid())
    subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    with open(p, "wb") as f:
        plistlib.dump(plist, f)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(p)], capture_output=True, text=True)
    if r.returncode != 0:
        print("launchctl bootstrap failed: {}".format(r.stderr.strip()), file=sys.stderr)
        return 1
    print("morning brief scheduled daily at {:02d}:{:02d} ({})".format(hh, mm, p))
    print("  log: {} · remove with: fitdash brief --uninstall".format(log))
    return 0


def uninstall() -> int:
    p = plist_path()
    subprocess.run(["launchctl", "bootout", "gui/{}/{}".format(os.getuid(), LABEL)], capture_output=True)
    if p.exists():
        p.unlink()
        print("morning brief removed")
    else:
        print("no morning brief was scheduled")
    return 0


def status() -> int:
    p = plist_path()
    if not p.exists():
        print("not scheduled (fitdash brief --install to set it up)")
        return 0
    with open(p, "rb") as f:
        pl = plistlib.load(f)
    t = pl.get("StartCalendarInterval") or {}
    loaded = subprocess.run(["launchctl", "print", "gui/{}/{}".format(os.getuid(), LABEL)],
                            capture_output=True).returncode == 0
    print("scheduled daily at {:02d}:{:02d} · {} · log {}".format(
        t.get("Hour", 0), t.get("Minute", 0), "loaded" if loaded else "NOT loaded", pl.get("StandardOutPath")))
    return 0
