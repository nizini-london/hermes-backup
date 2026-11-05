"""Server-render test with REAL react-dom/server (integrator addendum #3).

The stub-walker test (tests/test_desktop_render.py) proves the element
tree carries text; this one renders desktop/plugin.js with React 19's
own renderer and proves the shipped artifact produces non-empty HTML —
the class of bug that passed 9/9 syntax tests while the pane rendered
blank. React + react-dom are symlinked from the Hermes app's
node_modules (dependency-light, runs offline; skips cleanly when they
are absent).

Also pins the React "unique key" warning (array children without keys —
the pre-existing finding in BackupChip's dot/label spans and the row and
header children): zero key warnings is part of the pass criteria.

Run: /home/shaun/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_render.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from test_desktop_render import _find_node, _sdk_stub

REPO_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_PATH = REPO_ROOT / "desktop" / "plugin.js"
DRIVER_PATH = REPO_ROOT / "tests" / "test_render.mjs"

# React 19.x ships with the Hermes app; symlink instead of adding deps.
HOST_NODE_MODULES = Path("/home/shaun/.hermes/hermes-agent/node_modules")


def _happy_status() -> dict:
    now = int(time.time())
    return {
        "plugin": "hermes-backup",
        "interval_minutes": 5,
        "legs": [
            {"leg": "local", "configured": True, "reachable": True,
             "snapshot": {"id": "abcd1234", "epoch": now - 120}},
            {"leg": "r2", "configured": True, "reachable": True,
             "snapshot": {"id": "ef567890", "epoch": now - 180}},
        ],
        "last_run": {"epoch": now - 300, "status": "OK", "detail": "legs=2 ok=2"},
        "last_drill": {"epoch": now - 86400, "date": "2026-11-04",
                       "verdict": "PASS", "driver": "agent"},
        "schedule": {"kind": "systemd", "active": True, "next_run_epoch": now + 5400},
        "now": now,
    }


def test_pane_and_chip_server_render_with_real_react(tmp_path):
    node = _find_node()
    if node is None:
        pytest.skip("node not available for the server-render harness")
    for pkg in ("react", "react-dom"):
        if not (HOST_NODE_MODULES / pkg / "package.json").is_file():
            pytest.skip(f"{pkg} not present under {HOST_NODE_MODULES} (offline-safe)")

    scratch = tmp_path / "server-render"
    scratch.mkdir(parents=True)
    # Pinned module type: on Node without automatic module detection
    # (18 / 20.18), a bare .js copy of plugin.js loads as CommonJS and its
    # import statements fail before any assertion runs.
    (scratch / "package.json").write_text(json.dumps({"type": "module"}))
    sdk_pkg = scratch / "node_modules" / "@hermes" / "plugin-sdk"
    sdk_pkg.mkdir(parents=True)
    (sdk_pkg / "package.json").write_text(
        json.dumps({"name": "@hermes/plugin-sdk", "type": "module", "main": "index.js"}))
    (sdk_pkg / "index.js").write_text(_sdk_stub())
    for pkg in ("react", "react-dom"):
        os.symlink(HOST_NODE_MODULES / pkg, scratch / "node_modules" / pkg)
    shutil.copyfile(DESKTOP_PATH, scratch / "plugin.js")
    shutil.copyfile(DRIVER_PATH, scratch / "test_render.mjs")
    (scratch / "status.json").write_text(json.dumps(_happy_status()))

    proc = subprocess.run(
        [node, str(scratch / "test_render.mjs")],
        cwd=str(scratch), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"server render FAILED:\n{proc.stdout}\n{proc.stderr}"
    out = json.loads(proc.stdout)

    # The paste-worthy evidence, asserted: real react-dom/server HTML with
    # the widget's own text in it.
    for needle in ("Hermes Backup", "Local", "Offsite",
                   "Backup now", "Run drill", "RPO 5m", "next ~", "OK", "PASS"):
        assert needle in out["paneHtml"], f"pane HTML missing {needle!r}"
    assert "abcd1234" in out["paneHtml"]  # snapshot id column
    assert "backup" in out["chipHtml"] and "2m" in out["chipHtml"]
    assert out["keyWarningCount"] == 0, out["problems"]
    assert out["problems"] == []
