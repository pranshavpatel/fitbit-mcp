"""Local settings. All data and credentials live in FITBIT_MCP_HOME (default ~/.fitbit-mcp),
outside the project folder, so nothing sensitive can be committed by accident."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from . import catalog
from .util import private_dir

DEFAULT_LIMITS = {
    "takeout_max_total_bytes": 20 * 1024 ** 3,
    "takeout_max_file_bytes": 4 * 1024 ** 3,
    "takeout_max_files": 500_000,
    "takeout_max_ratio": 200,
}


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    home: Path
    config_path: Path
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.home / "fitbit.sqlite3"

    @property
    def credentials_dir(self) -> Path:
        return private_dir(self.home / "credentials")

    @property
    def raw_dir(self) -> Path:
        return private_dir(self.home / "raw")

    @property
    def takeout_dir(self) -> Path:
        return private_dir(self.home / "takeout")

    @property
    def exports_dir(self) -> Path:
        return private_dir(self.home / "exports")

    @property
    def logs_dir(self) -> Path:
        return private_dir(self.home / "logs")

    def limit(self, name: str) -> int:
        value = self.values.get(name, DEFAULT_LIMITS[name])
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(name + " must be a positive integer")
        return value

    # Provider configuration ---------------------------------------------------------
    def fitbit_client_id(self) -> str | None:
        value = os.environ.get("FITBIT_CLIENT_ID") or self.values.get("fitbit_client_id") or ""
        return None if not value or value.startswith("YOUR_") else value

    def fitbit_client_secret(self) -> str | None:
        return os.environ.get("FITBIT_CLIENT_SECRET") or self.values.get("fitbit_client_secret") or None

    def fitbit_redirect_uri(self) -> str:
        return self.values.get("fitbit_redirect_uri") or "http://127.0.0.1:8765/callback"

    # Google: by default reuse the `ghealth` CLI's setup, exactly as health-coach-app does ----
    def ghealth_dir(self) -> Path:
        raw = os.environ.get("FITBIT_MCP_GHEALTH_DIR") or self.values.get("ghealth_dir") or "~/.config/ghealth"
        return Path(raw).expanduser()

    def ghealth_credentials_path(self) -> Path:
        raw = os.environ.get("FITBIT_MCP_GHEALTH_CREDENTIALS") or self.values.get("ghealth_credentials")
        return Path(raw).expanduser() if raw else self.ghealth_dir() / "credentials.json"

    def google_client_secrets_path(self) -> Path:
        raw = os.environ.get("GOOGLE_CLIENT_SECRETS") or self.values.get("google_client_secrets")
        cli = self.ghealth_dir() / "client_secret.json"
        if raw:
            path = Path(raw).expanduser()
            path = path if path.is_absolute() else self.home / path
            # A configured path that doesn't exist (e.g. the example config's placeholder) falls back to the CLI's.
            return path if path.exists() or not cli.exists() else cli
        own = self.home / "client_secret.json"
        return own if own.exists() else cli

    def google_client(self) -> dict[str, str] | None:
        path = self.google_client_secrets_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ConfigError("Google client secrets file is not valid JSON.") from None
        section = data.get("installed") or data.get("web")
        if not isinstance(section, dict) or not section.get("client_id"):
            raise ConfigError("Google client secrets file needs an 'installed' (Desktop app) section with a "
                              "client_id.")
        return {"client_id": section["client_id"], "client_secret": section.get("client_secret", ""),
                "type": "installed" if "installed" in data else "web",
                "token_uri": section.get("token_uri") or "https://oauth2.googleapis.com/token"}

    def timezone(self):
        """IANA zone for civil-day boundaries (needed for physical-time filters)."""
        from zoneinfo import ZoneInfo
        candidates = [os.environ.get("FITBIT_MCP_TZ"), self.values.get("timezone")]
        toml_path = self.ghealth_dir() / "config.toml"
        if toml_path.exists():
            try:
                import tomllib  # Python 3.11+; optional, as in health-coach-app
                candidates.append(tomllib.loads(toml_path.read_text()).get("default", {}).get("timezone"))
            except Exception:
                pass
        localtime = Path("/etc/localtime")
        if localtime.is_symlink() and "zoneinfo/" in os.readlink(localtime):
            candidates.append(os.readlink(localtime).split("zoneinfo/", 1)[1])
        candidates += [os.environ.get("TZ"), "UTC"]
        for name in candidates:
            if not name:
                continue
            try:
                return ZoneInfo(name)
            except Exception:
                continue
        return ZoneInfo("UTC")

    def history_floor(self) -> date | None:
        raw = os.environ.get("FITBIT_MCP_HISTORY_FLOOR") or self.values.get("history_floor")
        return date.fromisoformat(raw) if raw else None

    def fitbit_shutdown(self) -> date:
        override = self.values.get("fitbit_api_shutdown_date")
        return date.fromisoformat(override) if override else catalog.FITBIT_SHUTDOWN

    def today(self) -> date:
        override = os.environ.get("FITBIT_MCP_TODAY")  # tests only
        return date.fromisoformat(override) if override else date.today()

    def inside_project_warning(self) -> str | None:
        project = os.environ.get("CLAUDE_PROJECT_DIR")
        if project:
            try:
                self.home.resolve().relative_to(Path(project).resolve())
                return ("FITBIT_MCP_HOME is inside the project folder; make sure it is git-ignored "
                        "so health data and tokens are never committed.")
            except ValueError:
                return None
        return None


def load_settings(home: str | Path | None = None) -> Settings:
    raw_home = home or os.environ.get("FITBIT_MCP_HOME") or "~/.fitbit-mcp"
    home_path = private_dir(Path(raw_home).expanduser().resolve())
    config_path = Path(os.environ.get("FITBIT_MCP_CONFIG") or home_path / "config.json").expanduser()
    values: dict[str, Any] = {}
    if config_path.exists():
        try:
            values = json.loads(config_path.read_text(encoding="utf-8"))
        except ValueError:
            raise ConfigError("{} is not valid JSON".format(config_path)) from None
        if not isinstance(values, dict):
            raise ConfigError("config.json must contain a JSON object")
    return Settings(home=home_path, config_path=config_path, values=values)
