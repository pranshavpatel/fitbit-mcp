"""Safe archive handling and Takeout format detection (SYNTHETIC archives only)."""
from __future__ import annotations

import io
import json
import os
import stat
import tarfile
import zipfile

import pytest

from fitbit_mcp import queries
from fitbit_mcp.server import _takeout
from fitbit_mcp.takeout import Limits, TakeoutError, extract, safe_member_path

from fakes import g_steps

LIMITS = Limits(max_total=50 * 1024 ** 2, max_file=10 * 1024 ** 2, max_files=1000, max_ratio=100)


def make_zip(path, members, symlinks=()):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        for name, target in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return path


def synthetic_takeout(tmp_path):
    base = "Takeout/Fitbit/Global Export Data/"
    members = {
        base + "steps-2026-10-01.json": json.dumps([
            {"dateTime": "10/01/26 08:00:00", "value": "120"}, {"dateTime": "10/01/26 08:01:00", "value": "80"}]),
        base + "heart_rate-2026-10-01.json": json.dumps([
            {"dateTime": "10/01/26 08:00:05", "value": {"bpm": 71, "confidence": 2}}]),
        base + "resting_heart_rate-2026-10-01.json": json.dumps([
            {"dateTime": "10/01/26 00:00:00", "value": {"date": "10/01/26", "value": 56.4, "error": 6.1}},
            {"dateTime": "10/02/26 00:00:00", "value": {"date": "10/02/26", "value": 0.0, "error": 0.0}}]),
        base + "weight-2026-10-01.json": json.dumps([
            {"logId": 9, "weight": 161.2, "bmi": 22.4, "date": "10/01/26", "time": "07:10:00", "source": "Aria"}]),
        base + "sleep-2026-10-01.json": json.dumps([
            {"logId": 31, "dateOfSleep": "2026-10-02", "startTime": "2026-10-01T23:00:00.000",
             "endTime": "2026-10-02T06:30:00.000", "minutesAsleep": 410, "minutesAwake": 40, "timeInBed": 450,
             "efficiency": 92, "isMainSleep": True, "levels": {"summary": {}}}]),
        "Takeout/Google Health/steps.json": json.dumps({"dataPoints": [g_steps("t1", "2026-10-03", 777)]}),
        "Takeout/Fitbit/Sleep/sleep_score.csv": "sleep_log_entry_id,timestamp,overall_score\n1,2026-10-02,80\n",
        "Takeout/Fitbit/Other/mystery.json": json.dumps({"unknownThing": 1}),
        "Takeout/archive_browser.html": "<html></html>",
    }
    return make_zip(tmp_path / "takeout-synthetic.zip", members, symlinks=[("Takeout/link", "/etc/passwd")])


def test_member_name_validation():
    for bad in ["../evil", "/abs/file", "a/../../b", "C:/windows/x", "a\\..\\..\\b", "x\x00y", ""]:
        with pytest.raises(TakeoutError):
            safe_member_path(bad)
    assert str(safe_member_path("Takeout/./Fitbit/a.json")) == "Takeout/Fitbit/a.json"


def test_zip_traversal_rejected_and_nothing_left_behind(tmp_path):
    archive = make_zip(tmp_path / "evil.zip", {"ok.json": "{}", "../../escape.json": "{}"})
    dest = tmp_path / "out"
    with pytest.raises(TakeoutError, match="traversal"):
        extract(archive, dest, LIMITS, lambda: None)
    assert not dest.exists() and not (tmp_path / "escape.json").exists()


def test_zip_bomb_ratio_and_size_limits(tmp_path):
    bomb = make_zip(tmp_path / "bomb.zip", {"big.json": b"0" * (5 * 1024 ** 2)})
    with pytest.raises(TakeoutError, match="ratio"):
        extract(bomb, tmp_path / "o1", LIMITS, lambda: None)
    small = Limits(max_total=1000, max_file=10_000, max_files=10, max_ratio=10_000)
    big = make_zip(tmp_path / "big.zip", {"a.json": "x" * 600, "b.json": "y" * 600})
    with pytest.raises(TakeoutError, match="size limit"):
        extract(big, tmp_path / "o2", small, lambda: None)
    many = make_zip(tmp_path / "many.zip", {"f{}.json".format(i): "{}" for i in range(12)})
    with pytest.raises(TakeoutError, match="file-count"):
        extract(many, tmp_path / "o3", small, lambda: None)


def test_tar_links_not_extracted(tmp_path):
    path = tmp_path / "t.tgz"
    with tarfile.open(path, "w:gz") as archive:
        data = b"{}"
        info = tarfile.TarInfo("Takeout/a.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("Takeout/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    extractor = extract(path, tmp_path / "out", LIMITS, lambda: None)
    assert (tmp_path / "out/part-1/Takeout/a.json").exists()
    assert not os.path.lexists(tmp_path / "out/part-1/Takeout/link")
    assert extractor.rejected and "link" in extractor.rejected[0][1]


def test_takeout_import_detects_formats_and_reports_unsupported(make_app, tmp_path):
    archive = synthetic_takeout(tmp_path)
    app = make_app()
    view = _takeout(app, str(archive))
    job = app.jobs.wait_for(view["job_id"], timeout=30)
    assert job["status"] == "succeeded"
    assert set(job["progress"]["providers_detected"]) == {"fitbit_takeout", "google_takeout"}
    assert job["progress"]["files_unsupported"] == 3
    steps = queries.query(app.store, metric="steps", provider="fitbit_takeout")["records"]
    assert [s["value"] for s in steps] == [120.0, 80.0]
    assert steps[0]["time_basis"] == "unknown_zone"           # zone not stated in the file: not guessed
    rhr = queries.query(app.store, metric="resting_heart_rate", provider="fitbit_takeout")["records"]
    assert [(r["local_date"], r["value"]) for r in rhr] == [("2026-10-01", 56.4)]    # 0.0 is missing, not zero
    weight = queries.query(app.store, metric="weight", provider="fitbit_takeout")["records"][0]
    assert weight["unit"] == "unknown"                         # account unit not stated: not assumed
    google = queries.query(app.store, metric="steps", provider="google_takeout")["records"][0]
    assert google["value"] == 777 and google["start_utc"] == "2026-10-03T14:00:00Z"
    unsupported = queries.data_types(app.store)["unsupported_or_unparsed"]
    reasons = " ".join(u["reason"] for u in unsupported)
    assert "CSV format not parsed" in reasons and "Unrecognized JSON structure" in reasons
    assert "symbolic link" in reasons
    assert any("treated as missing" in w for w in job["warnings"])
    # originals are retained on disk for auditing
    assert any(p.name == "sleep_score.csv" for p in app.settings.takeout_dir.rglob("*.csv"))


def test_takeout_reimport_is_idempotent(make_app, tmp_path):
    archive = synthetic_takeout(tmp_path)
    app = make_app()
    app.jobs.wait_for(_takeout(app, str(archive))["job_id"], timeout=30)
    with app.store.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    app.jobs.wait_for(_takeout(app, str(archive))["job_id"], timeout=30)
    with app.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == before


def test_unsafe_archive_fails_job_cleanly(make_app, tmp_path):
    archive = make_zip(tmp_path / "evil.zip", {"../../x.json": "{}"})
    app = make_app()
    job = app.jobs.wait_for(_takeout(app, str(archive))["job_id"], timeout=30)
    assert job["status"] == "failed" and "unsafe archive" in job["error"]
