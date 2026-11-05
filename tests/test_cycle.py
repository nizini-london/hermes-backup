"""Cycle tests: real tiny restic repo in /tmp, leg independence, exit-3
tolerance, single-instance guards, throttled prune, shadow-copy discipline."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import engine
from conftest import read_calls, write_state, TEST_PASSWORD


def make_cfg(tmp_path, source, local_repo=None, r2=False, **overrides):
    cfg = engine.Config(
        source_path=str(source),
        local_repo=str(local_repo) if local_repo else "",
        r2_endpoint="https://acct.r2.cloudflarestorage.com" if r2 else "",
        r2_bucket="hermes-test-bucket" if r2 else "",
        interval_minutes=5,
        **overrides)
    return cfg


def fresh_secrets():
    return engine.Secrets(restic_password=TEST_PASSWORD,
                          r2_access_key="test-access-key",
                          r2_secret_key="test-secret-key-0123")


def test_real_cycle_local_repo(data_root, tmp_path, plugin_env, source_tree, local_repo):
    """Full cycle against a REAL restic repo: ok verdict, snapshot id, shadow
    DBs snapshotted INSIDE the source tree, cleaned up afterwards, log verdict."""
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    assert result["verdict"] == "ok", result
    leg = result["legs"]["local"]
    assert leg["verdict"] == "ok"
    assert leg.get("snapshot_id"), leg
    assert not (source_tree / ".db-shadow").exists(), "shadow dir must be cleaned"

    # the shadow dir must have been INSIDE the snapshot (the first cold-review
    # defect: shadow DBs staged outside were never snapshotted)
    snap = leg["snapshot_id"]
    ls = subprocess.run(["restic", "--repo", str(local_repo), "ls", "-l", snap,
                         "--no-lock", "--json"],
                        capture_output=True, text=True,
                        env={**os.environ, "RESTIC_PASSWORD": TEST_PASSWORD,
                             "RESTIC_CACHE_DIR": os.environ["RESTIC_CACHE_DIR"]})
    assert ls.returncode == 0, ls.stderr
    paths = [json.loads(line).get("path", "")
             for line in ls.stdout.splitlines() if line.strip().startswith("{")]
    assert any(".db-shadow/state.db" in p for p in paths), paths
    assert any(p.endswith("config.yaml") for p in paths), paths
    assert not any("junk.bin" in p for p in paths), "excluded .cache leaked into snapshot"

    # verdict line in the log, parseable by the widget regex
    verdict = engine.last_run_verdict()
    assert verdict and verdict["verdict"] == "OK", verdict
    assert "local=1" in verdict["detail"]


def test_shadow_copy_wal_database(data_root, tmp_path, plugin_env, source_tree, local_repo):
    """Shadow copies must be valid, independent sqlite copies (WAL handled)."""
    import sqlite3
    report = engine.stage_shadow_dbs(source_tree)
    assert not report["failed"], report
    assert "state.db" in report["copied"]
    assert "profiles__default__state.db" in report["copied"]
    shadow_db = source_tree / ".db-shadow" / "state.db"
    assert shadow_db.is_file()
    check = sqlite3.connect(str(shadow_db)).execute("SELECT v FROM t WHERE k='probe'").fetchone()
    assert check == ("hello",)
    assert engine.cleanup_shadow_dbs(source_tree)
    assert not (source_tree / ".db-shadow").exists()


def test_shadow_fallback_for_broken_db(data_root, tmp_path, plugin_env, source_tree):
    """A non-sqlite *.db file falls back to cp (reference behavior), cycle proceeds."""
    (source_tree / "broken.db").write_bytes(b"not a sqlite database at all")
    report = engine.stage_shadow_dbs(source_tree)
    assert "broken.db" in report["fallback_copied"], report


def test_discover_skips_symlinked_dbs_and_dirs(data_root, tmp_path, source_tree):
    """Fix round 1 P2: discover_sqlite_dbs FOLLOWED symlinks — a link to a
    database outside the configured source was shadow-copied into the backup,
    silently pulling in outside data. Symlinks are now skipped (lstat view)."""
    from conftest import _mk_sqlite
    outside = tmp_path / "outside-data"
    outside.mkdir()
    _mk_sqlite(outside / "evil.db")       # the escape target
    _mk_sqlite(outside / "nested.db")     # behind a symlinked DIR
    (source_tree / "link.db").symlink_to(outside / "evil.db")
    (source_tree / "subdir-link").symlink_to(outside, target_is_directory=True)

    found = engine.discover_sqlite_dbs(source_tree)
    names = [p.name for p in found]
    assert "evil.db" not in names, names
    assert "nested.db" not in names, names
    assert all(str(source_tree) in str(p) for p in found), found

    report = engine.stage_shadow_dbs(source_tree)
    staged = report["copied"] + report["fallback_copied"]
    assert not any("evil" in f or "nested" in f for f in staged), staged


def test_exit3_tolerated(data_root, tmp_path, plugin_env, source_tree, local_repo,
                         restic_shim, shim_state):
    """restic rc 3 = source files vanished = tolerated warning, not failure."""
    write_state(shim_state, fake_all=True, backup_rc=3)
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    leg = result["legs"]["local"]
    assert leg["verdict"] == "ok", leg
    assert "exit code 3" in leg.get("warning", "")
    assert result["verdict"] == "ok"


def test_rc1_is_a_real_failure(data_root, tmp_path, plugin_env, source_tree,
                               local_repo, restic_shim, shim_state):
    write_state(shim_state, fake_all=True, backup_rc=1)
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    leg = result["legs"]["local"]
    assert leg["verdict"] == "fail"
    assert result["verdict"] == "fail"


def test_leg_independence_local_down_r2_runs(data_root, tmp_path, plugin_env,
                                             source_tree, restic_shim, shim_state,
                                             shim_calls):
    """Unmounted local repo → local=skip; the r2 leg MUST still run (the first
    cold-review defect: a down local leg starved the offsite leg)."""
    cfg = make_cfg(tmp_path, source_tree,
                   local_repo=tmp_path / "not-mounted" / "repo", r2=True)
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    assert result["legs"]["local"]["verdict"] == "skip"
    assert result["legs"]["r2"]["verdict"] == "ok"
    assert result["verdict"] == "warn"  # degraded, not failed
    calls = read_calls(shim_calls)
    backups = [c for c in calls if c["argv"] and c["argv"][0] == "backup"]
    assert len(backups) == 1
    assert backups[0]["repo"].startswith("s3:"), backups


def test_leg_independence_r2_down_local_runs(data_root, tmp_path, plugin_env,
                                             source_tree, local_repo,
                                             restic_shim, shim_state, shim_calls):
    write_state(shim_state, backup_rc=1)  # every s3: backup fails
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo, r2=True)
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    assert result["legs"]["r2"]["verdict"] == "fail"
    assert result["legs"]["local"]["verdict"] == "ok"
    assert result["verdict"] == "warn"


def test_r2_structural_check_every_cycle(data_root, tmp_path, plugin_env,
                                         source_tree, restic_shim, shim_state,
                                         shim_calls):
    """The second cold-review defect: the offsite repo was never checked.
    Structural r2 check runs EVERY cycle, throttled or not."""
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    assert result["verdict"] == "ok"
    checks = [c for c in read_calls(shim_calls) if c["argv"] and c["argv"][0] == "check"
              and c["repo"].startswith("s3:")]
    assert len(checks) == 1, checks
    assert "--no-lock" in checks[0]["argv"]


def test_prune_throttle_auto(data_root, tmp_path, plugin_env, source_tree,
                             local_repo, restic_shim, shim_state, shim_calls,
                             monkeypatch):
    """prune=auto prunes only in the top-of-hour window; outside it, no
    forget call — and the 2% data sample rides the same throttle."""
    write_state(shim_state, fake_all=True)  # deterministic fake for all repos
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)

    monkeypatch.setattr(engine, "_top_of_hour_window", lambda cfg, now=None: False)
    engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    assert not [c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]]
    assert not [c for c in read_calls(shim_calls)
                if c["argv"][:1] == ["check"]
                and any("--read-data-subset" in a for a in c["argv"])]

    write_state(shim_state, fake_all=True)  # keep fake mode for run 2
    monkeypatch.setattr(engine, "_top_of_hour_window", lambda cfg, now=None: True)
    result = engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    forgets = [c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]]
    assert len(forgets) == 1, forgets
    assert "--prune" in forgets[0]["argv"]
    assert "--keep-within" in forgets[0]["argv"]
    assert "--keep-last" in forgets[0]["argv"]
    samples = [c for c in read_calls(shim_calls)
               if c["argv"][:1] == ["check"]
               and any("--read-data-subset" in a for a in c["argv"])]
    assert len(samples) == 1, samples
    assert "2%" in " ".join(samples[0]["argv"])


def test_prune_force_and_skip(data_root, tmp_path, plugin_env, source_tree,
                              local_repo, restic_shim, shim_calls, monkeypatch):
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
    monkeypatch.setattr(engine, "_top_of_hour_window", lambda cfg, now=None: False)
    engine.run_cycle(cfg, prune="force", secrets=fresh_secrets())
    assert [c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]]
    calls_after_force = len(read_calls(shim_calls))
    engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    assert len(read_calls(shim_calls)) > calls_after_force  # cycle ran
    assert not [c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]][1:]


def test_cycle_skip_when_lock_busy(data_root, tmp_path, plugin_env, source_tree, local_repo):
    """Second concurrent cycle SKIPs cleanly (flock port: 'previous run active')."""
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
    lock = engine.acquire_cycle_lock()
    try:
        result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    finally:
        lock.close()
    assert result["skipped"] is True
    assert "SKIP" in result["reason"]
    assert result["verdict"] == "skip"


def test_cycle_skip_when_restic_running(data_root, tmp_path, plugin_env,
                                        source_tree, local_repo, real_pgrep):
    """A live restic process (real pgrep detection) makes the cycle fail
    CLEANLY, not block."""
    fake_bin = Path(engine.plugin_data_dir()) / "fake-restic-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    proc_path = fake_bin / "restic"
    shutil.copy2("/bin/sleep", proc_path)  # pgrep -x matches the file's name
    process = subprocess.Popen([str(proc_path), "30"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.3)
        real_pgrep()
        assert engine.restic_process_running()
        cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
        result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
        assert result["skipped"] is True
        assert "restic cycle is already running" in result["reason"]
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_cycle_no_wanted_legs_fails_with_setup_guidance(data_root, tmp_path,
                                                        plugin_env, source_tree):
    cfg = make_cfg(tmp_path, source_tree)
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    assert result["verdict"] == "fail"
    for leg_id in ("local", "r2"):
        assert result["legs"][leg_id]["verdict"] == "skip"


def test_hourly_maintenance_once_per_window(data_root, tmp_path, plugin_env,
                                            source_tree, local_repo,
                                            restic_shim, shim_state, shim_calls,
                                            monkeypatch):
    """Fix round 1 P2: prune='auto' ran forget --prune + the 2% sample on
    EVERY cycle inside the top-of-hour window (a 5-minute timer re-pruned 2-3
    times an hour). The maintenance pass now runs ONCE per window: the
    completed pass is recorded in state; repeats skip loudly. prune='force'
    still always prunes."""
    write_state(shim_state, fake_all=True)
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
    monkeypatch.setattr(engine, "_top_of_hour_window", lambda cfg, now=None: True)

    engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    assert len([c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]]) == 1
    assert engine._last_maintenance_epoch() is not None

    r2 = engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    forgets = [c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]]
    assert len(forgets) == 1, forgets  # repeat within the window: SKIPPED
    assert "already completed" in r2["detail"].get("prune_note", ""), r2["detail"]

    engine.run_cycle(cfg, prune="force", secrets=fresh_secrets())
    assert len([c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]]) == 2


def test_cycle_refuses_when_source_unresolvable(data_root, tmp_path, plugin_env,
                                                source_tree, local_repo,
                                                restic_shim, shim_state,
                                                shim_calls, monkeypatch):
    """Fix round 1 P1: a configured repo + unresolvable Hermes home used to
    back up the CWD ('restic backup .') and report success — protecting
    whatever directory the scheduler happened to start in. The cycle now
    REFUSES: loud failure, no restic backup, no success report."""
    import sys
    write_state(shim_state, fake_all=True)
    cfg = engine.Config(source_path="", local_repo=str(local_repo))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    assert result["verdict"] == "fail", result
    assert "source" in result["reason"].lower(), result
    assert not [c for c in read_calls(shim_calls) if c["argv"][:1] == ["backup"]], \
        "no restic backup may run without a resolved source"
    verdict = engine.last_run_verdict()
    assert verdict and verdict["verdict"] == "FAIL", verdict  # the loud report


def test_leg_backup_refuses_cwd_fallback(data_root, tmp_path, plugin_env,
                                         source_tree, local_repo,
                                         restic_shim, shim_state, shim_calls,
                                         monkeypatch):
    """Defense in depth for the same P1: _leg_backup itself never falls back
    to the CWD ('str(cfg.source or ".")') when the source cannot be resolved."""
    import sys
    write_state(shim_state, fake_all=True)
    cfg = engine.Config(source_path="", local_repo=str(local_repo))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    leg = engine.get_leg(cfg, "local", fresh_secrets())
    out = engine._leg_backup(leg, cfg)
    assert out["verdict"] == "fail", out
    assert "source" in out["reason"].lower(), out
    assert not [c for c in read_calls(shim_calls) if c["argv"][:1] == ["backup"]]


def test_password_never_in_output(data_root, tmp_path, plugin_env, source_tree,
                                  local_repo, restic_shim, shim_state):
    """A failing restic whose fake stderr embeds the password → scrubbed."""
    write_state(shim_state, fake_all=True, backup_rc=1,
                backup_out=f"boom {TEST_PASSWORD} leak")
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    dumped = json.dumps(result)
    assert TEST_PASSWORD not in dumped
    assert "<redacted>" in dumped  # the leaked line survives, the value does not
