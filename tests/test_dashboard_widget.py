"""Widget-slice tests: the dashboard status backend and the desktop SDK audit.

Covers dashboard/plugin_api.py (read-only restic status, numeric epochs,
60 s cache, rule-11 secret handling, log verdict parsing) and audits
desktop/plugin.js against the catalog's desktop-surface lint (rule 8).

The restic tests use a REAL tiny repository in a tmp dir (restic is small
and fast); the "leaking shim" test uses a fake restic on PATH that echoes
the password into stderr, proving the scrubber catches it end-to-end.

Run: /home/shaun/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_dashboard_widget.py -q
(any pytest-capable python works: fastapi is stubbed if absent, and every
hermes-core import inside the module degrades gracefully).

All hermes/secret-scope access is monkeypatched per test — the suite never
touches the real profile's config, secrets, or plugin-data directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "dashboard" / "plugin_api.py"
DESKTOP_PATH = REPO_ROOT / "desktop" / "plugin.js"
MANIFEST_PATH = REPO_ROOT / "dashboard" / "manifest.json"

TEST_PASSWORD = "widget-test-password-xyz"

ISO_STRING_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


# ------------------------------------------------------------------ loader --

def _load_plugin_api(tmp_path, monkeypatch):
    """Import dashboard/plugin_api.py as a module, with cache/log paths
    isolated to tmp_path and fastapi stubbed when not installed."""
    try:
        import fastapi  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fastapi")

        class _Router:
            def __init__(self):
                self.routes = {}

            def get(self, path, **_kw):
                def deco(fn):
                    self.routes[("GET", path)] = fn
                    return fn

                return deco

            def post(self, path, **_kw):
                def deco(fn):
                    self.routes[("POST", path)] = fn
                    return fn

                return deco

        stub.APIRouter = _Router
        sys.modules.setdefault("fastapi", stub)

    spec = importlib.util.spec_from_file_location(
        "hermes_backup_dashboard_plugin_api", MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Isolate every filesystem side of the module inside tmp_path.
    monkeypatch.setattr(mod, "_hermes_home", lambda: Path(tmp_path))
    return mod


def _settings_config(settings: dict) -> dict:
    """A config.yaml snapshot shaped like hermes stores plugin settings."""
    return {"plugins": {"entries": {"hermes-backup": {"settings": settings}}}}


def _assert_no_iso_strings(node) -> None:
    """The status contract is numeric epochs; renderers do the math."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                assert not ISO_STRING_RE.search(value), f"{key}={value!r}"
            else:
                _assert_no_iso_strings(value)
    elif isinstance(node, list):
        for value in node:
            _assert_no_iso_strings(value)
    elif isinstance(node, str):
        assert not ISO_STRING_RE.search(node), node


# ---------------------------------------------------- real restic repository --

@pytest.fixture(scope="module")
def tiny_repo(tmp_path_factory):
    """A real (tiny) restic repo with one snapshot, built with real restic."""
    base = tmp_path_factory.mktemp("restic-widget")
    repo = base / "repo"
    src = base / "src"
    src.mkdir()
    (src / "note.txt").write_text("a backup you haven't restored is a hypothesis\n")
    env = dict(os.environ, RESTIC_PASSWORD=TEST_PASSWORD)
    subprocess.run(["restic", "-r", str(repo), "init"], env=env,
                   capture_output=True, text=True, check=True)
    subprocess.run(["restic", "-r", str(repo), "backup", str(src)], env=env,
                   capture_output=True, text=True, check=True)
    return repo


# ------------------------------------------------------------------- tests --

def test_status_against_real_restic_repo(tmp_path, monkeypatch, tiny_repo):
    """End-to-end: configured leg answers with a numeric-epoch snapshot; the
    unconfigured offsite leg is an independent skip."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_config_snapshot",
                        lambda: _settings_config({"local_repo": str(tiny_repo),
                                                  "interval_minutes": 60}))
    monkeypatch.setattr(
        mod, "_secret",
        lambda name: TEST_PASSWORD if name == "HERMES_BACKUP_RESTIC_PASSWORD" else None)

    payload = mod.status()

    local, r2 = payload["legs"][0], payload["legs"][1]
    assert local["leg"] == "local" and local["configured"] and local["reachable"]
    snap = local["snapshot"]
    assert isinstance(snap["epoch"], int) and snap["epoch"] > 1_000_000_000
    assert isinstance(snap["id"], str) and len(snap["id"]) >= 7
    # leg independence: the offsite leg being unconfigured never gated local
    assert r2["configured"] is False and r2["reachable"] is False
    assert "backup_setup" in (r2["error"] or "")
    assert payload["interval_minutes"] == 60.0
    assert isinstance(payload["now"], int) and payload["now"] > 1_000_000_000
    assert payload["schedule"]["kind"] in {"systemd", "cron", "none"}
    _assert_no_iso_strings(payload)
    assert TEST_PASSWORD not in json.dumps(payload)


def test_status_cache_serves_within_ttl(tmp_path, monkeypatch):
    """60 s server-side cache: repeat widget polls do not rebuild the payload
    (no restic subprocesses per tick); expiry rebuilds."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_config_snapshot", lambda: _settings_config({}))
    monkeypatch.setattr(mod, "_secret", lambda name: None)

    calls = {"n": 0}
    real_build = mod._build_payload

    def counting_build():
        calls["n"] += 1
        return real_build()

    monkeypatch.setattr(mod, "_build_payload", counting_build)

    mod.status()
    mod.status()
    assert calls["n"] == 1  # second poll inside the TTL was served from cache

    cache = mod._cache_path()
    cached = json.loads(cache.read_text("utf-8"))
    cached["ts"] -= mod.CACHE_TTL_SECONDS + 1
    cache.write_text(json.dumps(cached), "utf-8")

    mod.status()
    assert calls["n"] == 2
    assert json.loads(cache.read_text("utf-8"))["ts"] > cached["ts"]


def test_last_run_verdict_survives_progress_flood(tmp_path, monkeypatch):
    """Verdict parsing over a LARGE tail: 500 restic progress lines, then the
    verdict, then more progress — a small tail would miss it (incident rule)."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    log = mod._log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = (["[0:04] 100.00%  25 MiB/s  ETA 0s"] * 500
             + ["[2026-11-05 08:00:00] OK legs=2 ok=2 warn=0 fail=0"]
             + ["[0:05] 100.00%"] * 20)
    log.write_text("\n".join(lines) + "\n", "utf-8")

    run = mod._last_run(log)
    assert run is not None
    assert run["status"] == "OK"
    assert isinstance(run["epoch"], int) and run["epoch"] > 1_000_000_000
    assert "legs=2" in run["detail"]

    # a progress line is never mistaken for a verdict
    assert mod._last_run(log.parent / "missing.log") is None
    empty_log = log.parent / "empty.log"
    empty_log.write_text("[0:04] 100.00%  25 MiB/s  ETA 0s\n", "utf-8")
    assert mod._last_run(empty_log) is None


def test_last_drill_parsing(tmp_path, monkeypatch):
    """The DRILL lines backup_drill appends parse to verdict + date."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    log = mod._log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "[2026-11-04 10:00:00] OK legs=2 ok=2\n"
        "DRILL 2026-11-04 PASS driver=agent\n"
        "DRILL 2026-11-05 FAIL driver=agent\n",
        "utf-8",
    )
    drill = mod._last_drill(log)
    assert drill["verdict"] == "FAIL"
    assert drill["date"] == "2026-11-05"
    assert drill["driver"] == "agent"
    assert isinstance(drill["epoch"], int) and drill["epoch"] > 1_000_000_000


def test_secret_never_returned_even_when_restic_echoes_it(tmp_path, monkeypatch):
    """A fake restic on PATH prints the password into stderr: the response
    must arrive scrubbed, and the process env must stay clean."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "restic"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"snapshots\" ]; then\n"
        f"  echo 'Fatal: wrong password: {TEST_PASSWORD}' >&2\n"
        "  exit 10\n"
        "fi\n"
        "exit 1\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(mod, "_config_snapshot",
                        lambda: _settings_config({"local_repo": str(tmp_path / "any-repo")}))
    monkeypatch.setattr(
        mod, "_secret",
        lambda name: TEST_PASSWORD if name == "HERMES_BACKUP_RESTIC_PASSWORD" else None)

    payload = mod.status()
    local, r2 = payload["legs"][0], payload["legs"][1]

    assert local["configured"] is True and local["reachable"] is False
    assert "***" in (local["error"] or "")
    assert TEST_PASSWORD not in json.dumps(payload)
    # leg independence: local's failure left the offsite leg untouched (skip)
    assert r2["configured"] is False
    assert "RESTIC_PASSWORD" not in os.environ


def test_restic_env_drops_inherited_credentials(tmp_path, monkeypatch):
    """Env hygiene: stale inherited restic/S3 credential names are dropped and
    only resolved values are set — a wrong-profile value can never ride along."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    monkeypatch.setenv("RESTIC_PASSWORD", "stale-inherited-value")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "stale-key")
    env = mod._restic_env("s3:endpoint/bucket", "resolved-pw", "resolved-key", "resolved-secret")
    assert env["RESTIC_PASSWORD"] == "resolved-pw"
    assert env["AWS_ACCESS_KEY_ID"] == "resolved-key"
    assert env["AWS_SECRET_ACCESS_KEY"] == "resolved-secret"
    assert env["RESTIC_REPOSITORY"] == "s3:endpoint/bucket"
    local_env = mod._restic_env("/local/repo", "resolved-pw", None, None)
    assert "AWS_ACCESS_KEY_ID" not in local_env


def test_plugin_settings_matches_key_or_manifest_name(tmp_path, monkeypatch):
    """Settings are found under the registry key OR the manifest name, with
    the legacy ``config`` subtree still honored."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod, "_config_snapshot",
        lambda: {"plugins": {"entries": {"some-folder-key": {
            "name": mod.PLUGIN_ID,
            "settings": {"interval_minutes": 7},
            "config": {"local_repo": "/legacy/path"},
        }}}})
    settings = mod._plugin_settings()
    assert settings["interval_minutes"] == 7
    assert settings["local_repo"] == "/legacy/path"  # legacy fallback present


def test_widget_files_are_debranded():
    """No Venus / NIZINI branding anywhere in the widget slice."""
    for path in (DESKTOP_PATH, MODULE_PATH, MANIFEST_PATH):
        text = path.read_text("utf-8")
        assert not re.search(r"venus|nizini", text, re.IGNORECASE), f"{path.name} still branded"


def test_desktop_plugin_stays_inside_the_sdk_surface():
    """Catalog rule 8, checked with the catalog's own lint: the desktop
    half produces ZERO findings, and imports only the allowed specifiers."""
    try:
        from hermes_cli.plugin_validate_desktop import desktop_surface_findings
    except ImportError:
        pytest.skip("hermes_cli.plugin_validate_desktop not importable here")
    findings = desktop_surface_findings(DESKTOP_PATH.read_text("utf-8"))
    assert findings == [], findings

    allowed = {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
    for line in DESKTOP_PATH.read_text("utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("import "):
            continue
        match = (re.search(r"from\s+['\"]([^'\"]+)['\"]", stripped)
                 or re.match(r"import\s+['\"]([^'\"]+)['\"]", stripped))
        assert match is not None and match.group(1) in allowed, stripped


def test_desktop_plugin_is_loadable_esm():
    """node --check parses the file as CommonJS-wrapped ESM syntax (the app
    loads it uncompiled — a syntax error would fail the load at runtime)."""
    node = None
    for candidate in ("node", "/usr/bin/node"):
        try:
            proc = subprocess.run([candidate, "--version"], capture_output=True, text=True)
            if proc.returncode == 0:
                node = candidate
                break
        except OSError:
            continue
    if node is None:
        pytest.skip("node not available for an ESM syntax check")
    # Rename so node treats the ESM file as a module (no package.json type).
    with_node = DESKTOP_PATH.parent / "_plugin_esm_check.mjs"
    with_node.write_text(DESKTOP_PATH.read_text("utf-8"), "utf-8")
    try:
        proc = subprocess.run([node, "--check", str(with_node)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    finally:
        with_node.unlink(missing_ok=True)
