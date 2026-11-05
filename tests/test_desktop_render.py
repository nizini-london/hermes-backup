"""Render-level tests for desktop/plugin.js (the P1 #1 regression class).

The pre-existing widget tests never asserted RENDERED output — `node
--check` proves syntax, not semantics — which is exactly how the P1 #1
bug shipped 9/9 green: text passed as the THIRD jsx() argument (the
element KEY, not children) rendered an empty element everywhere.

This harness renders the pane and the statusbar chip for real in Node:
desktop/plugin.js is loaded as ESM (copied as a .mjs AND a scratch
package.json pins "type": "module" — belt and braces, so the copy loads
as ESM on EVERY supported Node, with or without automatic module
detection), with a stub `react/jsx-runtime` implementing the REAL react
contract (children ride in props; the third argument is the key) and a
stub `@hermes/plugin-sdk` serving the injected status. The rendered
element trees are then resolved by a micro-renderer and the visible
text is asserted.

Zero external dependencies: the stubs are generated inline by this test
— nothing is symlinked or installed, so it runs offline on any machine
with a node binary. The ONLY skip is when node itself is missing (same
as the whole render suite).

Run: /home/shaun/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_desktop_render.py -q
(needs node on PATH; skips with a clear reason otherwise)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_PATH = REPO_ROOT / "desktop" / "plugin.js"


def _find_node() -> str | None:
    """Shared discovery logic (the session fixture lives in conftest.py);
    also unit-pinned by test_find_node_survives_a_nodeless_path."""
    for candidate in ("node", "/usr/bin/node"):
        try:
            proc = subprocess.run([candidate, "--version"], capture_output=True, text=True)
            if proc.returncode == 0:
                return candidate
        except OSError:
            continue
    return None


def _jsx_runtime_stub() -> str:
    return """\
// Stub with the REAL react contract: children ride in props; the third
// argument is the element key. This is what catches P1 #1 — text passed
// as the third argument becomes a KEY and never appears as rendered text.
// React's dev key-warning classes are surfaced as testable violations via
// globalThis.__KEY_VIOLATIONS__ (asserted empty by tests/test_render.py):
//  - a key INSIDE the props object (React 19 dev: "A props object
//    containing a key prop is being spread into JSX");
//  - array children without keys are caught in the micro-renderer walk.
export function jsx(type, config, maybeKey) {
  if (config && config.key !== undefined) {
    ;(globalThis.__KEY_VIOLATIONS__ = globalThis.__KEY_VIOLATIONS__ ?? []).push(
      'key inside props object (React 19 dev: spread-key warning) — ' +
      'pass the key as the third jsx() argument'
    )
  }
  return {
    $$typeof: Symbol.for('react.element'),
    type,
    key: maybeKey === undefined ? null : maybeKey,
    props: { ...(config ?? {}) },
  }
}
export function jsxs(type, config, maybeKey) {
  return jsx(type, config, maybeKey)
}
"""


def _sdk_stub() -> str:
    return """\
// Stub of the @hermes/plugin-sdk surface used by desktop/plugin.js.
// Badge/Button/Tip are pass-throughs so a REAL react-dom/server render
// (tests/test_render.mjs) still emits the widget's own text; the walker
// test treats them as opaque element types either way.
export function StatusDot() { return null }
export function Badge({ children }) { return children ?? null }
export function Button({ children }) { return children ?? null }
export function Tip({ children }) { return children ?? null }
export const haptic = () => {}
export const host = {
  composer: { submit: () => true },
  notify: () => {},
}
export function useQuery() {
  const raw = globalThis.__BACKUP_STATUS_JSON__ ?? 'null'
  let data = null
  try { data = JSON.parse(raw) } catch { data = null }
  return { data, isError: false, error: null }
}
"""


DRIVER = """\
import plugin from './plugin-under-test.mjs'

// The status under test arrives via env (BACKUP_STATUS_JSON); the SDK
// stub's useQuery serves it from this global. Set before any render.
globalThis.__BACKUP_STATUS_JSON__ = process.env.BACKUP_STATUS_JSON ?? 'null'
globalThis.__KEY_VIOLATIONS__ = []

const registered = []
const ctx = { register: (cfg) => registered.push(cfg) }
plugin.register(ctx)

// Like real React: function components are INVOKED during the walk so their
// subtree materializes (LegRow would otherwise be an opaque element with no
// children). The SDK stubs are opaque by design — their widget-supplied
// props.children is walked instead of calling them.
const SDK_TYPES = new Set(['StatusDot', 'Badge', 'Button', 'Tip'])

function walk(node, out, depth) {
  if (node === null || node === undefined || typeof node === 'boolean') return
  if (Array.isArray(node)) {
    // React: "Each child in a list should have a unique key prop" — every
    // ELEMENT in an array needs a key (strings/numbers/nulls do not).
    for (const child of node) {
      if (child && typeof child === 'object' && child.$$typeof &&
          (child.key === null || child.key === undefined)) {
        globalThis.__KEY_VIOLATIONS__.push(
          'array child without key: ' +
          (typeof child.type === 'function' ? child.type.name : String(child.type))
        )
      }
    }
    node.forEach((n) => walk(n, out, depth))
    return
  }
  if (typeof node === 'string' || typeof node === 'number') {
    out.push(String(node))
    return
  }
  if (typeof node === 'object') {
    if (typeof node.type === 'function' && !SDK_TYPES.has(node.type.name) && depth < 10) {
      walk(node.type(node.props ?? {}), out, depth + 1)
      return
    }
    if (node.type !== null && node.type !== undefined) {
      const name = typeof node.type === 'function' ? node.type.name : String(node.type)
      // StatusDot tones are chip/pane state, not text — carried as a token
      // so tests can assert the health color semantics.
      if (name === 'StatusDot' && node.props && node.props.tone) {
        out.push('@tone:' + node.props.tone)
      }
      out.push('@' + name)
    }
    if (node.props && typeof node.props === 'object') walk(node.props.children, out, depth + 1)
  }
}

function flat(cfg) {
  const out = []
  const el = cfg.render()          // jsx(Pane, {})
  walk(el.type(el.props ?? {}), out, 0)  // invoke the component function
  return out
}

const paneCfg = registered.find((r) => r.id === 'pane')
const chipCfg = registered.find((r) => r.id === 'chip')
if (!paneCfg || !chipCfg) {
  process.stderr.write('driver: pane/chip not registered\\n')
  process.exit(1)
}
function chipTones() {
  return flat(chipCfg)
    .filter((t) => t.startsWith('@tone:'))
    .map((t) => t.slice('@tone:'.length))
}

process.stdout.write(JSON.stringify({
  registered: registered.map((r) => r.id),
  pane: flat(paneCfg),
  chip: flat(chipCfg),
  chipTones: chipTones(),
  keyViolations: globalThis.__KEY_VIOLATIONS__ ?? [],
}))
"""


def _render(tmp_path: Path, node: str, status: dict) -> dict:
    """Load plugin.js in a scratch node project and return the flattened
    render output {registered, pane, chip}."""
    scratch = tmp_path / "node-render"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    # Pinned module type: on Node without automatic module detection
    # (18 / 20.18), a bare .js copy of plugin.js loads as CommonJS and its
    # import statements fail before any assertion runs.
    (scratch / "package.json").write_text(json.dumps({"type": "module"}))
    sdk_pkg = scratch / "node_modules" / "@hermes" / "plugin-sdk"
    react_pkg = scratch / "node_modules" / "react"
    sdk_pkg.mkdir(parents=True)
    react_pkg.mkdir(parents=True)
    (sdk_pkg / "package.json").write_text(
        json.dumps({"name": "@hermes/plugin-sdk", "type": "module", "main": "index.js"}))
    (sdk_pkg / "index.js").write_text(_sdk_stub())
    (react_pkg / "package.json").write_text(json.dumps({
        "name": "react", "type": "module",
        "exports": {"./jsx-runtime": "./jsx-runtime.js"}}))
    (react_pkg / "jsx-runtime.js").write_text(_jsx_runtime_stub())
    # Copy as .mjs (ESM on every supported Node — even without automatic
    # module detection) AND ship a package.json pinning "type": "module":
    # either alone suffices, together nothing can load the copy as CJS.
    shutil.copyfile(DESKTOP_PATH, scratch / "plugin-under-test.mjs")
    (scratch / "driver.mjs").write_text(DRIVER)
    env = dict(os.environ, BACKUP_STATUS_JSON=json.dumps(status))
    proc = subprocess.run(
        [node, str(scratch / "driver.mjs")],
        capture_output=True, text=True, env=env, cwd=str(scratch), timeout=60,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout)


def _text(tokens: list) -> str:
    return " ".join(t for t in tokens if not t.startswith("@"))


def _assert_every_text_token_visible(tokens: list) -> None:
    """P1 #1: no rendered text token may be empty/whitespace — a token list
    that once carried 'Offsite' must still carry it as real text."""
    for token in tokens:
        if token.startswith("@"):
            continue
        assert token.strip() != "", f"empty text token rendered: {tokens!r}"


NOW = int(time.time())  # real "now": ages/countdowns asserted with bucket tolerance


def test_p1_pane_renders_visible_label_and_value_text(tmp_path, node_available):
    """P1 #1 regression, the exact bug: with text passed as the third jsx()
    argument every label, value and button rendered EMPTY. The rendered pane
    must contain the leg labels, snapshot ages, facts and button texts."""
    if node_available is None:
        pytest.skip("node not available for the render harness")
    status = {
        "plugin": "hermes-backup",
        "interval_minutes": 5,
        "legs": [
            {"leg": "local", "configured": True, "reachable": True,
             "snapshot": {"id": "abcd1234", "epoch": NOW - 120}},
            {"leg": "r2", "configured": True, "reachable": True,
             "snapshot": {"id": "ef567890", "epoch": NOW - 180}},
        ],
        "last_run": {"epoch": NOW - 300, "status": "OK", "detail": "legs=2 ok=2"},
        "last_drill": {"epoch": NOW - 86400, "date": "2026-11-04",
                       "verdict": "PASS", "driver": "agent"},
        "schedule": {"kind": "systemd", "active": True, "next_run_epoch": NOW + 5400},
        "now": NOW,
    }
    out = _render(tmp_path, node_available, status)
    assert out["registered"] == ["pane", "chip"]

    pane = _text(out["pane"])
    chip = _text(out["chip"])
    _assert_every_text_token_visible(out["pane"])
    _assert_every_text_token_visible(out["chip"])

    # Leg labels (rendered as EMPTY spans under the bug)
    assert "Local" in pane and "Offsite" in pane
    # Snapshot ages ("… ago" values — empty under the bug)
    assert "2m ago" in pane and "3m ago" in pane
    # Facts + buttons (empty under the bug)
    assert "Last cycle" in pane and "OK" in pane
    assert "Last drill" in pane and "PASS" in pane
    assert "Backup now" in pane and "Run drill" in pane
    # Header + RPO badge
    assert "Hermes Backup" in pane and "RPO 5m" in pane
    # P2 #7: future next-run is a real countdown, never "~0s"
    assert "timer active" in pane
    assert re.search(r"next ~\d+[smhd]", pane), f"no countdown rendered: {pane!r}"
    assert "next ~0s" not in pane
    # Chip shows a snapshot age too (empty under the bug)
    assert re.search(r"backup \d+[smhd]", chip), f"chip label missing: {chip!r}"


def test_p1_pane_renders_stalled_leg_as_partial_status(tmp_path, node_available):
    """P1 #2 renderer side: a stalled leg shows as 'probing…' with its
    honest error detail while the other leg's answer is fully visible —
    a slow leg never blanks the pane."""
    if node_available is None:
        pytest.skip("node not available for the render harness")
    status = {
        "plugin": "hermes-backup",
        "interval_minutes": 5,
        "legs": [
            {"leg": "local", "configured": True, "reachable": True,
             "snapshot": {"id": "abcd1234", "epoch": NOW - 120}},
            {"leg": "r2", "configured": True, "reachable": False, "stalled": True,
             "error": "probe timed out after 8s — leg did not answer in time"},
        ],
        "last_run": {"epoch": NOW - 300, "status": "OK", "detail": "legs=2 ok=2"},
        "last_drill": None,
        "schedule": {"kind": "none", "active": False, "next_run_epoch": None},
        "now": NOW,
    }
    out = _render(tmp_path, node_available, status)
    pane = _text(out["pane"])
    assert "Local" in pane and "2m ago" in pane       # healthy leg still visible
    assert "Offsite" in pane and "probing…" in pane   # stalled leg, partial status
    assert "probe timed out after 8s" in pane         # honest reason, not blank
    _assert_every_text_token_visible(out["pane"])


def test_p2_empty_repo_renders_no_snapshots_yet(tmp_path, node_available):
    """P2 #5 renderer side: a reachable repo with zero snapshots renders as
    'no snapshots yet' (warn tone) — never as a failure/red 'unreachable'."""
    if node_available is None:
        pytest.skip("node not available for the render harness")
    status = {
        "plugin": "hermes-backup",
        "interval_minutes": 5,
        "legs": [
            {"leg": "local", "configured": True, "reachable": True, "snapshot": None},
        ],
        "last_run": None,
        "last_drill": None,
        "schedule": {"kind": "none", "active": False, "next_run_epoch": None},
        "now": NOW,
    }
    out = _render(tmp_path, node_available, status)
    pane = _text(out["pane"])
    assert "Local" in pane
    assert "no snapshots yet" in pane
    assert "run backup_now for the first snapshot" in pane
    assert "unreachable" not in pane


def _chip_status(legs: list) -> dict:
    now = int(time.time())
    return {
        "plugin": "hermes-backup", "interval_minutes": 5, "legs": legs,
        "last_run": None, "last_drill": None,
        "schedule": {"kind": "none", "active": False, "next_run_epoch": None},
        "now": now,
    }


def test_p2_chip_tone_stalled_is_warn_not_bad(tmp_path, node_available):
    """Greptile round-2 P2 regression: the chip must agree with the pane's
    'probing…' semantics — a stalled (unfinished) check warns; only a real
    failure (unreachable, not stalled) or RPO-overdue freshness paints it
    bad. Asserted on the StatusDot tone the chip renders."""
    if node_available is None:
        pytest.skip("node not available for the render harness")
    fresh = {"leg": "local", "configured": True, "reachable": True,
             "snapshot": {"id": "abcd1234", "epoch": int(time.time()) - 120}}
    stalled = {"leg": "r2", "configured": True, "reachable": False, "stalled": True,
               "error": "probe timed out after 8s — leg did not answer in time"}
    unreachable = {"leg": "r2", "configured": True, "reachable": False,
                   "error": "connection refused"}
    overdue = dict(fresh, snapshot={"id": "abcd1234",
                                    "epoch": int(time.time()) - 4000})

    scenarios = [
        # (name, legs, expected tone, forbidden tone)
        ("fresh local + stalled offsite → warn", [fresh, stalled], "warn", "bad"),
        ("real unreachable leg → bad", [fresh, unreachable], "bad", None),
        ("all healthy and fresh → good", [fresh], "good", None),
        ("overdue freshness + stalled → bad (real RPO breach wins)",
         [overdue, stalled], "bad", None),
    ]
    for name, legs, expected, forbidden in scenarios:
        out = _render(tmp_path, node_available, _chip_status(legs))
        chip_tones = [t for t in out["chip"] if t.startswith("@tone:")]
        assert chip_tones == [f"@tone:{expected}"], f"{name}: {chip_tones}"
        if forbidden:
            assert f"@tone:{forbidden}" not in chip_tones, name


def test_p2_past_next_run_renders_due_now_not_zero(tmp_path, node_available):
    """P2 #7 edge: a next-run timestamp already in the past (timer just
    elapsed) renders 'due now' — not '~0s'."""
    if node_available is None:
        pytest.skip("node not available for the render harness")
    status = {
        "plugin": "hermes-backup",
        "interval_minutes": 5,
        "legs": [],
        "last_run": None,
        "last_drill": None,
        "schedule": {"kind": "systemd", "active": True, "next_run_epoch": NOW - 60},
        "now": NOW,
    }
    out = _render(tmp_path, node_available, status)
    pane = _text(out["pane"])
    assert "due now" in pane
    assert "next ~" not in pane


def test_find_node_survives_a_nodeless_path(monkeypatch):
    """Harness honesty: on a machine without node the finder returns None
    (→ the render tests skip) instead of raising."""
    def boom(*_a, **_k):
        raise OSError("no node on this machine")

    monkeypatch.setattr(subprocess, "run", boom)
    assert _find_node() is None
