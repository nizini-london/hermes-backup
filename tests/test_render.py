"""Zero-dependency render tests (integrator round-2 recipe) — never skip.

The previous version of this file rendered with REAL react-dom/server and
symlinked react from a machine-specific path — wherever that path was
absent the test silently skipped, a false green. Everything here needs
ONLY the node binary (any supported version: the widget copy is a .mjs
and the scratch pins "type": "module"):

- a real-contract `react/jsx-runtime` shim (children ride in props; the
  third argument is the key; a key inside the props object is recorded
  as a violation — React 19 dev's spread-key warning class),
- the `@hermes/plugin-sdk` stub keeps the fake hook context the recipe
  calls for: the widget's only hook, useQuery, is served from injected
  status; useState/useEffect are not used by the widget, and their
  no-op equivalents in the stub cover first-render branches,
- a micro-renderer invokes the component functions and recursively
  resolves the element tree, collecting every string child and checking
  that every ELEMENT child of an array carries a key (React: "Each
  child in a list should have a unique key prop").

Asserted here (round-2 findings, both classes of React key warning, and
the first-render branches the recipe names):

- no status at all → pane renders `loading…`, chip renders `backup —`
  (muted tone) — the first-render branches,
- every render path is free of key violations (unique-key AND
  spread-key classes), including the happy, stalled and due-now paths.

There are NO external dependencies — the stubs are generated inline in
tests/test_desktop_render.py and nothing is symlinked or installed — so
these tests run offline on any machine with node and CANNOT skip for a
dependency reason. The only legitimate skip in the render suite is a
missing node binary (documented there).

Run: python3 -m pytest tests/test_render.py -q
"""

from __future__ import annotations

import time

import pytest

from test_desktop_render import _render  # noqa: F401  (harness reuse; fixture: conftest)


def _status(legs, *, schedule=None, last_run=None, last_drill=None):
    now = int(time.time())
    return {
        "plugin": "hermes-backup",
        "interval_minutes": 5,
        "legs": legs,
        "last_run": last_run,
        "last_drill": last_drill,
        "schedule": schedule or {"kind": "none", "active": False, "next_run_epoch": None},
        "now": now,
    }


def test_first_render_branches_without_status(tmp_path, node_available):
    """First-render branches the recipe names: before /status answers, the
    pane shows `loading…` and the chip shows its `backup —` default with a
    muted dot — real, non-empty text, never blank."""
    if node_available is None:
        pytest.skip("node not available for the render harness")
    out = _render(tmp_path, node_available, None)
    pane = " ".join(t for t in out["pane"] if not t.startswith("@"))
    chip = " ".join(t for t in out["chip"] if not t.startswith("@"))
    assert "loading…" in pane, f"pane first render: {pane!r}"
    assert "backup —" in chip, f"chip first render: {chip!r}"
    assert out["chipTones"] == ["muted"]
    assert out["keyViolations"] == []


def test_every_render_path_is_free_of_key_violations(tmp_path, node_available):
    """Both React key-warning classes stay dead on EVERY render path:
    no array child without a key, no key inside a props object."""
    if node_available is None:
        pytest.skip("node not available for the render harness")
    now = int(time.time())
    healthy = {"leg": "local", "configured": True, "reachable": True,
               "snapshot": {"id": "abcd1234", "epoch": now - 120}}
    stalled = {"leg": "r2", "configured": True, "reachable": False, "stalled": True,
               "error": "probe timed out after 8s — leg did not answer in time"}
    empty_repo = {"leg": "local", "configured": True, "reachable": True,
                  "snapshot": None}
    paths = {
        "first render (no status)": None,
        "happy": _status([healthy, dict(healthy, leg="r2",
                                        snapshot={"id": "ef567890", "epoch": now - 180})],
                         schedule={"kind": "systemd", "active": True,
                                   "next_run_epoch": now + 5400},
                         last_run={"epoch": now - 300, "status": "OK",
                                   "detail": "legs=2 ok=2"},
                         last_drill={"epoch": now - 86400, "date": "2026-11-04",
                                     "verdict": "PASS", "driver": "agent"}),
        "stalled offsite": _status([healthy, stalled]),
        "empty repo": _status([empty_repo]),
        "due now": _status([healthy],
                           schedule={"kind": "systemd", "active": True,
                                     "next_run_epoch": now - 60}),
    }
    for name, status in paths.items():
        out = _render(tmp_path, node_available, status)
        assert out["keyViolations"] == [], f"{name}: {out['keyViolations']}"
