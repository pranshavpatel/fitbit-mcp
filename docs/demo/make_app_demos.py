#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13", "playwright==1.49.1"]
# ///
"""Phone-app screenshots for the docs, from SYNTHETIC data, rendered by WebKit as an iPhone 14.

    uv run --script docs/demo/make_app_demos.py      # first run: uv run --with playwright==1.49.1 playwright install webkit

Uses the same throwaway data home as make_demos.py (never your ~/.fitbit-mcp) and the real app server.
"""
from __future__ import annotations

import shutil
import sys
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_demos as MD  # noqa: E402  (sets FITBIT_MCP_HOME to a temp dir and builds sys.path)

import appserver as A  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

TABS = ("today", "sleep", "train", "log", "trends")


def main() -> int:
    MD.make_home()
    # pin "now" to the demo's moment so the synthetic day looks like today
    real_dt = A.datetime

    class FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return MD.NOW if tz is None else MD.NOW.astimezone(tz)

    with mock.patch.object(A, "datetime", FixedDT), mock.patch("data.datetime", FixedDT):
        srv = A.make_server(MD.HOME, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:{}".format(srv.server_address[1])
        with sync_playwright() as p:
            browser = p.webkit.launch()
            page = browser.new_page(**p.devices["iPhone 14"])
            for tab in TABS:
                page.goto(base + "/#" + tab)
                page.wait_for_selector(".card")
                page.wait_for_timeout(400)
                page.screenshot(path=str(MD.HERE / "app_{}.png".format(tab)))
                print("wrote docs/demo/app_{}.png".format(tab))
            browser.close()
        srv.shutdown()
    shutil.rmtree(MD.HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
