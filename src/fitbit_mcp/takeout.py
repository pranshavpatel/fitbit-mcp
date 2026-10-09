"""Safe Takeout / Fitbit data-export handling.

Extraction protections: no absolute paths, drive letters, '..' components, NUL bytes,
symlinks, hard links or device files; per-file, total-size, file-count and compression
ratio limits (enforced on the bytes actually written, not just header claims); output
only inside a fresh directory under the data home.

Format detection is file-by-file from actual content. Supported today:
  * Google Health API-shaped JSON (objects with `dataPoints`, or DataPoint lists)
  * legacy Fitbit "Global Export Data" JSON files named steps-, calories-, heart_rate-,
    resting_heart_rate-, sleep- and weight-YYYY-MM-DD.json, validated by structure.
Everything else (CSV, unrecognized JSON, other files) is kept on disk and reported as
unsupported with the reason; nothing is silently discarded.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from .parsers import fitbit as fitbit_parser
from .parsers import google as google_parser
from .parsers.base import DATE_ONLY, UNKNOWN_ZONE, ParseResult, Rec
from .util import to_number

ARCHIVE_SUFFIXES = (".zip", ".tgz", ".tar.gz", ".tar")
MAX_JSON_PARSE_BYTES = 512 * 1024 ** 2
LEGACY_FILE = re.compile(r"^(steps|calories|heart_rate|resting_heart_rate|sleep|weight)-(\d{4}-\d{2}-\d{2})\.json$")


class TakeoutError(Exception):
    pass


class Limits:
    def __init__(self, max_total: int, max_file: int, max_files: int, max_ratio: int):
        self.max_total, self.max_file, self.max_files, self.max_ratio = max_total, max_file, max_files, max_ratio


def safe_member_path(name: str) -> PurePosixPath:
    """Validate an archive member name and return a safe relative path or raise."""
    if not name or "\x00" in name:
        raise TakeoutError("empty or NUL-containing member name")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise TakeoutError("absolute path in archive: " + name[:120])
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise TakeoutError("path traversal in archive: " + name[:120])
    if not parts:
        raise TakeoutError("empty member path")
    return PurePosixPath(*parts)


def _target(dest: Path, relative: PurePosixPath) -> Path:
    target = (dest / Path(*relative.parts)).resolve()
    target.relative_to(dest.resolve())  # raises ValueError if outside
    return target


class Extractor:
    def __init__(self, dest: Path, limits: Limits, checkpoint: Callable[[], None]):
        self.dest, self.limits, self.checkpoint = dest, limits, checkpoint
        self.total = 0
        self.files = 0
        self.rejected: list[tuple[str, str]] = []

    def _copy_stream(self, source, relative: PurePosixPath, declared: int | None) -> None:
        self.files += 1
        if self.files > self.limits.max_files:
            raise TakeoutError("archive exceeds the file-count limit ({})".format(self.limits.max_files))
        try:
            target = _target(self.dest, relative)
        except ValueError:
            raise TakeoutError("unsafe archive rejected: path escapes destination") from None
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        written = 0
        try:
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            self.rejected.append((str(relative)[:200], "duplicate member name; first copy kept"))
            return
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                self.total += len(chunk)
                if written > self.limits.max_file or (declared is not None and written > declared):
                    out.close()
                    target.unlink()
                    raise TakeoutError("member larger than allowed or than declared: " + str(relative)[:120])
                if self.total > self.limits.max_total:
                    out.close()
                    target.unlink()
                    raise TakeoutError("extracted data exceeds the total size limit")
                out.write(chunk)
        self.checkpoint()

    def zip(self, archive: Path, prefix: str) -> None:
        try:
            handle = zipfile.ZipFile(archive)
        except zipfile.BadZipFile:
            raise TakeoutError("not a valid ZIP archive: " + archive.name) from None
        with handle:
            infos = handle.infolist()
            if len(infos) + self.files > self.limits.max_files:
                raise TakeoutError("archive exceeds the file-count limit")
            if sum(i.file_size for i in infos) + self.total > self.limits.max_total:
                raise TakeoutError("archive's declared size exceeds the total size limit")
            for info in infos:
                if info.is_dir():
                    continue
                try:
                    relative = PurePosixPath(prefix) / safe_member_path(info.filename)
                except TakeoutError as error:
                    raise TakeoutError("unsafe archive rejected: {}".format(error)) from None
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    self.rejected.append((info.filename[:200], "symbolic link not extracted"))
                    continue
                if info.flag_bits & 0x1:
                    self.rejected.append((info.filename[:200], "encrypted member not extracted"))
                    continue
                if info.file_size > self.limits.max_file:
                    self.rejected.append((info.filename[:200], "member exceeds per-file size limit"))
                    continue
                if info.compress_size and info.file_size > 1024 * 1024 and \
                        info.file_size / info.compress_size > self.limits.max_ratio:
                    raise TakeoutError("suspicious compression ratio (possible zip bomb): " + info.filename[:120])
                with handle.open(info) as source:
                    self._copy_stream(source, relative, info.file_size)

    def tar(self, archive: Path, prefix: str) -> None:
        try:
            handle = tarfile.open(archive, mode="r:*")
        except tarfile.TarError:
            raise TakeoutError("not a valid tar archive: " + archive.name) from None
        with handle:
            for member in handle:
                if member.isdir():
                    continue
                try:
                    relative = PurePosixPath(prefix) / safe_member_path(member.name)
                except TakeoutError as error:
                    raise TakeoutError("unsafe archive rejected: {}".format(error)) from None
                if not member.isfile():
                    self.rejected.append((member.name[:200], "link or special file not extracted"))
                    continue
                if member.size > self.limits.max_file:
                    self.rejected.append((member.name[:200], "member exceeds per-file size limit"))
                    continue
                source = handle.extractfile(member)
                if source is None:
                    continue
                with source:
                    self._copy_stream(source, relative, member.size)

    def directory(self, source: Path, prefix: str) -> None:
        root = source.resolve()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                if path.is_symlink() or not path.is_file():
                    self.rejected.append((rel[:200], "link or special file not copied"))
                    continue
                size = path.stat().st_size
                if size > self.limits.max_file:
                    self.rejected.append((rel[:200], "file exceeds per-file size limit"))
                    continue
                with open(path, "rb") as stream:
                    self._copy_stream(stream, PurePosixPath(prefix) / safe_member_path(rel), size)


def is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_SUFFIXES)


def extract(source: Path, dest: Path, limits: Limits, checkpoint: Callable[[], None]) -> Extractor:
    source = source.expanduser()
    if not source.exists():
        raise TakeoutError("path does not exist: " + str(source))
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    extractor = Extractor(dest, limits, checkpoint)
    try:
        if source.is_file():
            if not is_archive(source):
                raise TakeoutError("expected a .zip, .tgz/.tar.gz or a folder")
            (extractor.zip if source.name.lower().endswith(".zip") else extractor.tar)(source, "part-1")
        elif source.is_dir():
            parts = sorted(p for p in source.iterdir() if p.is_file() and not p.is_symlink() and is_archive(p))
            if parts:
                for index, part in enumerate(parts, 1):
                    (extractor.zip if part.name.lower().endswith(".zip") else extractor.tar)(part, "part-{}".format(index))
            else:
                extractor.directory(source, "folder")
        else:
            raise TakeoutError("unsupported path type")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # never leave a half-extracted unsafe tree behind
        raise
    return extractor


def iter_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name


# ------------------------------------------------------------------------- parsing

def _legacy_time(text: str) -> datetime:
    return datetime.strptime(text, "%m/%d/%y %H:%M:%S")


def _legacy_minute_series(kind: str, items: list, rel: str) -> ParseResult:
    out = ParseResult()
    metric, unit = ("steps", "count") if kind == "steps" else ("calories_out", "kcal")
    for item in items:
        moment = _legacy_time(item["dateTime"])
        out.records.append(Rec(category="activity", metric=metric, granularity="interval",
                               record_key=moment.isoformat(), value=to_number(item.get("value")), unit=unit,
                               local_date=moment.date().isoformat(), start_local=moment.isoformat(),
                               time_basis=UNKNOWN_ZONE, quality=["timezone_not_stated_in_file"]))
    return out


def _legacy_heart(items: list, rel: str) -> ParseResult:
    out = ParseResult()
    for item in items:
        moment = _legacy_time(item["dateTime"])
        value = item.get("value") or {}
        out.records.append(Rec(category="heart", metric="heart_rate", granularity="sample",
                               record_key=moment.isoformat(), value=to_number(value.get("bpm")), unit="bpm",
                               local_date=moment.date().isoformat(), start_local=moment.isoformat(),
                               time_basis=UNKNOWN_ZONE, quality=["timezone_not_stated_in_file"],
                               details={"confidence": value.get("confidence")}))
    return out


def _legacy_resting(items: list, rel: str) -> ParseResult:
    out = ParseResult()
    skipped = 0
    for item in items:
        value = item.get("value") or {}
        number = to_number(value.get("value"))
        if not number:  # Fitbit writes 0 / null for days without an estimate: missing, not zero
            skipped += 1
            continue
        day = datetime.strptime(value.get("date") or item["dateTime"][:8], "%m/%d/%y").date().isoformat()
        out.records.append(Rec(category="heart", metric="resting_heart_rate", granularity="daily", record_key=day,
                               value=number, unit="bpm", local_date=day, time_basis=DATE_ONLY, payload=item,
                               details={"error": value.get("error")}))
    if skipped:
        out.warnings.append("{}: {} day(s) without a resting heart rate estimate were treated as missing"
                            .format(rel, skipped))
    return out


def _legacy_weight(items: list, rel: str) -> ParseResult:
    out = ParseResult()
    for item in items:
        day = datetime.strptime(item["date"], "%m/%d/%y").date().isoformat()
        key = str(item.get("logId") or "{}T{}".format(day, item.get("time")))
        common = dict(category="body", granularity="sample", record_key=key, local_date=day,
                      start_local="{}T{}".format(day, item["time"]) if item.get("time") else None,
                      time_basis=UNKNOWN_ZONE, source_id=key, payload=item,
                      data_source={"source": item.get("source")} if item.get("source") else None)
        if item.get("weight") is not None:
            out.records.append(Rec(metric="weight", value=to_number(item["weight"]), unit="unknown",
                                   quality=["unit_unknown_account_setting"], **common))
        if item.get("bmi") is not None:
            out.records.append(Rec(metric="bmi", value=to_number(item["bmi"]), unit="kg/m2", **common))
    return out


def _looks_like_google(data: Any) -> bool:
    points = data.get("dataPoints") if isinstance(data, dict) else data
    return isinstance(points, list) and bool(points) and all(isinstance(p, dict) for p in points[:5]) and \
        any("dataSource" in p or str(p.get("name", "")).startswith("users/") for p in points[:5])


def _google_kind(point: dict) -> str | None:
    name = str(point.get("name", ""))
    match = re.match(r"^users/[^/]+/dataTypes/([^/]+)/dataPoints/", name)
    if match:
        return match.group(1)
    bodies = [k for k, v in point.items() if k not in ("name", "dataSource") and isinstance(v, dict)]
    if len(bodies) == 1:
        return re.sub(r"(?<!^)(?=[A-Z])", "-", bodies[0]).lower()
    return None


def parse_file(path: Path, rel: str) -> tuple[str | None, ParseResult]:
    """Returns (provider_tag, result). provider_tag None means unsupported."""
    out = ParseResult()
    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                header = handle.readline().strip()[:200]
        except OSError:
            header = "?"
        out.unsupported.append(("csv", "CSV format not parsed (header: {})".format(header)))
        return None, out
    if suffix != ".json":
        out.unsupported.append(("file", "File type '{}' not parsed".format(suffix or "none")))
        return None, out
    if path.stat().st_size > MAX_JSON_PARSE_BYTES:
        out.unsupported.append(("json", "JSON file too large to parse safely"))
        return None, out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        out.unsupported.append(("json", "Invalid JSON"))
        return None, out
    if _looks_like_google(data):
        points = data["dataPoints"] if isinstance(data, dict) else data
        for point in points:
            kind = _google_kind(point) if isinstance(point, dict) else None
            if not kind:
                out.unsupported.append(("google", "Data point type could not be determined"))
                continue
            out.extend(google_parser.parse_datapoint(kind, point, provider_tag="google_takeout"))
        return "google_takeout", out
    match = LEGACY_FILE.match(path.name)
    if match and isinstance(data, list):
        kind = match.group(1)
        try:
            if kind in ("steps", "calories"):
                result = _legacy_minute_series(kind, data, rel)
            elif kind == "heart_rate":
                result = _legacy_heart(data, rel)
            elif kind == "resting_heart_rate":
                result = _legacy_resting(data, rel)
            elif kind == "weight":
                result = _legacy_weight(data, rel)
            else:  # sleep logs share the Web API sleep log structure
                result = fitbit_parser.parse("sleep", {"sleep": data})
                for rec in result.records:
                    rec.scope_key = None
            for rec in result.records:
                rec.scope_key = None  # takeout snapshots never tombstone other records
            return "fitbit_takeout", result
        except (KeyError, TypeError, ValueError, AttributeError):
            out.unsupported.append((kind, "File name matched '{}' but its structure did not; not parsed"
                                    .format(kind)))
            return None, out
    keys = sorted(data.keys())[:8] if isinstance(data, dict) else ["<list>" if isinstance(data, list) else "?"]
    out.unsupported.append(("json", "Unrecognized JSON structure (top-level: {})".format(",".join(map(str, keys)))))
    return None, out
