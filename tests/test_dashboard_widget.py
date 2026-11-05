"""Widget-slice tests: the dashboard status backend and the desktop SDK audit.

Covers dashboard/plugin_api.py (read-only restic status, numeric epochs,
60 s cache, rule-11 secret handling, log verdict parsing) and audits
desktop/plugin.js against the catalog's desktop-surface lint (rule 8).

The restic tests use a REAL tiny repository in a tmp dir (restic is small
and fast); the "leaking shim" test uses a fake restic on PATH that echoes
the password into stderr, proving the scrubber catches it end-to-end.

De-branding is verified by grep over the shipped files (deliberately not a
test here: the forbidden pattern's own literals would self-hit every
de-brand grep).

Run: python -m pytest tests/test_dashboard_widget.py -q (any pytest-capable
python: fastapi is stubbed if absent, and every hermes-core import inside
the module degrades gracefully).

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
import threading
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "dashboard" / "plugin_api.py"
DESKTOP_PATH = REPO_ROOT / "desktop" / "plugin.js"

TEST_PASSWORD = "widget-test-password-xyz"

# The restic/S3 credential env names dashboard/plugin_api.py::_restic_env()
# drops from the child env (cross-reference that list) — the names a status()
# call must never WRITE into the process env (QA S4).
_CREDENTIAL_ENV_NAMES = frozenset({
    "RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND",
    "RESTIC_REPOSITORY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
})

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

    def counting_build(*_a, **_k):
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


def test_slow_probe_reports_partial_inside_client_deadline(tmp_path, monkeypatch):
    """P1 #2 regression: one stalled leg must never push /status past the
    desktop client's 30 s deadline. The offsite leg's restic stalls; the
    response still ships — local leg answered, offsite reported ``stalled``
    (partial status), and the whole request stays far inside 30 s."""
    import time as _time

    mod = _load_plugin_api(tmp_path, monkeypatch)
    # Shrink the budget so the test itself stays fast: attempt cap 1 s,
    # overall budget 3 s (production values: 8 s / 22 s).
    monkeypatch.setattr(mod, "PROBE_ATTEMPT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(mod, "COLD_BUDGET_SECONDS", 3.0)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "restic"
    # The offsite (s3:) repo hangs; the local repo answers. (Keyed on the
    # s3: prefix, not a path substring: the pytest tmp dir contains the test
    # name, so any "slow"/"fast" match would hit both legs.)
    shim.write_text(
        "#!/bin/sh\n"
        'case "$RESTIC_REPOSITORY" in\n'
        "  s3:*) sleep 5; echo '[]' ;;\n"
        "  *) echo '[{\"short_id\":\"abcd1234\",\"time\":\"2026-11-05T08:00:00Z\"}]' ;;\n"
        "esac\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(
        mod, "_config_snapshot",
        lambda: _settings_config({
            "local_repo": str(tmp_path / "fast-repo"),
            "r2_endpoint": "slow-endpoint.example",
            "r2_bucket": "slow-bucket",
        }))
    monkeypatch.setattr(
        mod, "_secret",
        lambda name: TEST_PASSWORD if name == "HERMES_BACKUP_RESTIC_PASSWORD" else None)

    started = _time.monotonic()
    payload = mod.status()
    elapsed = _time.monotonic() - started

    local, r2 = payload["legs"][0], payload["legs"][1]
    # The fast leg's answer still ships in full.
    assert local["reachable"] is True and local["snapshot"]["id"] == "abcd1234"
    # The stalled leg is honest partial status, not a blank pane and not a
    # false "unreachable" verdict.
    assert r2["reachable"] is False
    assert r2["stalled"] is True
    assert "timed out" in (r2["error"] or "")
    # ...and the whole cold request finished inside the client deadline.
    assert elapsed < 15, f"cold /status took {elapsed:.1f}s (client gives 30 s)"
    assert TEST_PASSWORD not in json.dumps(payload)


def test_empty_repo_is_reachable_not_unreachable(tmp_path, monkeypatch):
    """P2 #5 regression: a repo that answers with zero snapshots is
    reachable — "no snapshots yet" — never falsely "repository unreachable"."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "restic"
    shim.write_text("#!/bin/sh\necho '[]'\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(mod, "_config_snapshot",
                        lambda: _settings_config({"local_repo": str(tmp_path / "fresh-repo")}))
    monkeypatch.setattr(
        mod, "_secret",
        lambda name: TEST_PASSWORD if name == "HERMES_BACKUP_RESTIC_PASSWORD" else None)

    payload = mod.status()
    local = payload["legs"][0]
    assert local["reachable"] is True          # the repo answered
    assert local["snapshot"] is None           # nothing backed up yet — a fact, not an error
    assert not local.get("error")              # and NOT "repository unreachable"
    assert "unreachable" not in json.dumps(payload)


def test_cache_stamp_records_build_completion_time(tmp_path, monkeypatch):
    """P2 #3 regression: the cache entry is stamped when the build FINISHES.
    A slow _build_payload used to be recorded with its start time — born
    already expired, so every later poll rebuilt during outages."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_config_snapshot", lambda: _settings_config({}))
    monkeypatch.setattr(mod, "_secret", lambda name: None)

    def slow_build(*_a, **_k):
        time.sleep(1.2)
        return {"plugin": mod.PLUGIN_ID, "legs": [], "interval_minutes": 5.0,
                "last_run": None, "last_drill": None,
                "schedule": {"kind": "none", "active": False, "next_run_epoch": None},
                "now": int(time.time())}

    monkeypatch.setattr(mod, "_build_payload", slow_build)

    started = time.time()
    payload = mod.status()
    cache = mod._cache_path()
    cached = json.loads(cache.read_text("utf-8"))
    # Stamped at (or after) completion — not with the pre-build time.
    assert cached["ts"] >= started + 1.0, (
        f"cache ts {cached['ts']:.2f} predates build completion "
        f"(started {started:.2f}) — slow build born expired")
    assert cached["data"] == payload


def test_concurrent_misses_share_one_build(tmp_path, monkeypatch):
    """P2 #4 regression: concurrent cold misses are single-flight — exactly
    one build runs, every caller gets the same payload, the cache file stays
    valid JSON, and no shared fixed-name .tmp is left behind or torn."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_config_snapshot", lambda: _settings_config({}))
    monkeypatch.setattr(mod, "_secret", lambda name: None)

    calls = {"n": 0}
    counter_lock = threading.Lock()

    def slow_build(*_a, **_k):
        with counter_lock:
            calls["n"] += 1
        time.sleep(0.5)
        return {"plugin": mod.PLUGIN_ID, "legs": [], "interval_minutes": 5.0,
                "last_run": None, "last_drill": None,
                "schedule": {"kind": "none", "active": False, "next_run_epoch": None},
                "now": int(time.time())}

    monkeypatch.setattr(mod, "_build_payload", slow_build)

    n_callers = 4
    barrier = threading.Barrier(n_callers)
    results = []

    def poll():
        barrier.wait()
        results.append(mod.status())

    threads = [threading.Thread(target=poll) for _ in range(n_callers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1, f"built {calls['n']}x — concurrent misses raced"
    assert len(results) == n_callers
    assert all(r == results[0] and r["plugin"] == mod.PLUGIN_ID for r in results)
    cache = mod._cache_path()
    cached = json.loads(cache.read_text("utf-8"))  # still valid JSON (no torn write)
    assert cached["data"] == results[0]
    assert list(cache.parent.glob("*.tmp")) == []  # unique tmp per write, all replaced


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


def test_drill_verdict_pinned_to_engine_alphabet(tmp_path, monkeypatch):
    """QA N4b regression: _DRILL_RE accepts only the verdicts the engine's
    LOG_DRILL_RE writes (PASS|FAIL) — a malformed line must not render as a
    drill verdict (display-only before), and a longer word (PASSING) must
    not half-match PASS."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    log = mod._log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "DRILL 2026-11-01 WEIRD driver=agent\n"     # never a verdict the engine writes
        "DRILL 2026-11-02 PASSING driver=agent\n"   # prefix must not half-match
        "DRILL 2026-11-03 PASS driver=agent\n",     # the real one still parses
        "utf-8",
    )
    drill = mod._last_drill(log)
    assert drill["verdict"] == "PASS"
    assert drill["date"] == "2026-11-03"
    assert drill["driver"] == "agent"

    bogus = log.parent / "bogus.log"
    bogus.write_text("DRILL 2026-11-04 WEIRD driver=agent\n", "utf-8")
    assert mod._last_drill(bogus) is None          # bogus verdict → "no drill recorded"


def test_schedule_prefers_active_timer_then_cron(tmp_path, monkeypatch):
    """P2 #6 regression: preference order is active timer > active cron >
    installed-but-inactive timer > none. An INACTIVE timer used to be
    returned before cron was ever consulted, hiding an active Hermes cron."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    inactive = {"kind": "systemd", "active": False, "next_run_epoch": None}
    active_timer = {"kind": "systemd", "active": True, "next_run_epoch": 1700000000}
    cron = {"kind": "cron", "active": True, "next_run_epoch": 1700000001}

    monkeypatch.setattr(mod, "_systemd_timer", lambda: inactive)
    monkeypatch.setattr(mod, "_cron_job", lambda: cron)
    assert mod._schedule() == cron  # a disabled timer must not hide the cron

    monkeypatch.setattr(mod, "_systemd_timer", lambda: active_timer)
    assert mod._schedule() == active_timer  # an active timer still wins

    monkeypatch.setattr(mod, "_systemd_timer", lambda: inactive)
    monkeypatch.setattr(mod, "_cron_job", lambda: None)
    assert mod._schedule() == inactive  # installed-but-off is reported honestly

    monkeypatch.setattr(mod, "_systemd_timer", lambda: None)
    assert mod._schedule() == {"kind": "none", "active": False, "next_run_epoch": None}


def test_active_timer_without_next_elapse_reports_none(tmp_path, monkeypatch):
    """P2 #8 regression: an active timer whose next elapse is unreadable
    reports next_run_epoch=None — never an invented next-5-minute-boundary
    run (that guess was wrong for every interval except 5 minutes)."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "systemctl"
    # Active, but no parseable NextElapseUSecRealtime property.
    shim.write_text("#!/bin/sh\necho 'ActiveState=active'\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))

    schedule = mod._schedule()
    assert schedule == {"kind": "systemd", "active": True, "next_run_epoch": None}
    assert schedule["next_run_epoch"] is None  # not a fabricated epoch


def test_hermes_cron_is_visible_when_no_systemd_unit_exists(tmp_path, monkeypatch):
    """The cron path stays reachable end-to-end: no systemd unit (shim exits
    1) + an enabled Hermes cron job referencing the plugin's tools → the
    schedule is the cron job with its numeric epoch next run."""
    mod = _load_plugin_api(tmp_path, monkeypatch)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "systemctl"
    shim.write_text("#!/bin/sh\nexit 1\n")  # unit does not exist
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps([{"id": "nightly", "enabled": True,
                     "next_run_at": "2026-11-06T02:00:00+00:00",
                     "command": "hermes cron: run backup_now"}]),
        "utf-8",
    )

    schedule = mod._schedule()
    assert schedule["kind"] == "cron" and schedule["active"] is True
    assert isinstance(schedule["next_run_epoch"], int) and schedule["next_run_epoch"] > 1e9


def test_secret_never_returned_even_when_restic_echoes_it(tmp_path, monkeypatch):
    """A fake restic on PATH prints the password into stderr: the response
    must arrive scrubbed, and status() must not WRITE any credential name
    into the process env (asserted relative to a pre-call snapshot — QA S4)."""
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

    before = frozenset(os.environ)  # ambient env, incl. any inherited credential
    payload = mod.status()
    local, r2 = payload["legs"][0], payload["legs"][1]

    assert local["configured"] is True and local["reachable"] is False
    assert "***" in (local["error"] or "")
    assert TEST_PASSWORD not in json.dumps(payload)
    # leg independence: local's failure left the offsite leg untouched (skip)
    assert r2["configured"] is False
    # Env hygiene, scoped to status()'s own effect (QA S4): the call must
    # not ADD any credential name to the process env. An absolute
    # ``"RESTIC_PASSWORD" not in os.environ`` assert flickers on
    # multi-tenant hosts whose ambient env legitimately carries such a
    # name (this host runs a production restic stack) — that inherited
    # presence is not this module's doing. The drop-inherited-values
    # property is asserted separately by
    # test_restic_env_drops_inherited_credentials.
    added = set(os.environ) - before
    leaky = added & _CREDENTIAL_ENV_NAMES
    assert not leaky, (
        f"status() wrote credential env names into the process env: {sorted(leaky)}")


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


def test_desktop_plugin_is_loadable_esm(tmp_path):
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
    assert node is not None  # skip() above returns — narrow for the checker
    # Rename so node treats the ESM file as a module (no package.json type).
    # The copy lives in tmp_path, NOT the source tree: a hard kill mid-test
    # would otherwise litter desktop/ (QA N-6).
    with_node = tmp_path / "_plugin_esm_check.mjs"
    with_node.write_text(DESKTOP_PATH.read_text("utf-8"), "utf-8")
    proc = subprocess.run([node, "--check", str(with_node)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
