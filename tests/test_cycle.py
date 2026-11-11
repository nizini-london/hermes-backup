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


def _spawn_fake_restic(dirname: str, repository: str) -> subprocess.Popen:
    """A fake `restic` process: /bin/bash copied under the name restic —
    pgrep -x matches the file's name (comm), and bash running
    ``sleep 30; sleep 30`` stays alive under it. (/bin/sleep would NOT: it is
    a coreutils multicall binary on modern distros — exec'd as 'restic' it
    exits instantly; the pre-fix test only passed when the host's own
    production restic happened to be running.) RESTIC_REPOSITORY is injected
    like the engine's own Leg.env — the repository travels via env, not
    argv."""
    fake_bin = Path(engine.plugin_data_dir()) / dirname
    fake_bin.mkdir(parents=True, exist_ok=True)
    proc_path = fake_bin / "restic"
    shutil.copy2("/bin/bash", proc_path)
    return subprocess.Popen([str(proc_path), "-c", "sleep 30; sleep 30"],
                            env={**os.environ, "RESTIC_REPOSITORY": repository},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_cycle_skip_when_restic_runs_for_our_repo(data_root, tmp_path, plugin_env,
                                                  source_tree, local_repo, real_guard):
    """A live restic process FOR OUR repository blocks the cycle cleanly
    (repo-scoped guard: its RESTIC_REPOSITORY references this repo)."""
    process = _spawn_fake_restic("fake-restic-ours", str(local_repo))
    try:
        time.sleep(0.3)
        real_guard()
        cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
        result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
        assert result["skipped"] is True
        assert "already running" in result["reason"], result
        assert str(local_repo) in result["reason"], result
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_cycle_runs_despite_foreign_restic_process(data_root, tmp_path, plugin_env,
                                                   source_tree, real_guard,
                                                   restic_shim, shim_state):
    """Drill-round-1 finding: a restic process for a DIFFERENT repository (a
    foreign production stack on the same machine, different home) must NOT
    block this config's cycle — the old bare ``pgrep restic`` did. The
    r2-only config additionally covers the ``s3:`` repo-identifier matching
    path, and the faked shim keeps the cycle restic-free (fast)."""
    process = _spawn_fake_restic("fake-restic-foreign",
                                 "s3:https://other.example.com/other-bucket")
    try:
        time.sleep(0.3)
        real_guard()
        cfg = make_cfg(tmp_path, source_tree, r2=True)
        result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
        assert result.get("skipped") is not True, result
        assert result["verdict"] in ("ok", "warn", "fail"), result
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_cycle_skip_when_our_repo_holds_lock_files(data_root, tmp_path, plugin_env,
                                                   source_tree, local_repo, real_guard):
    """Restic's own lock files in OUR local repo block the cycle — the
    primary repo-scoped signal; no running process is required. The name
    mirrors restic's REAL lock layout (Greptile round-1 P1): extensionless
    64-character hex ids under locks/ — NOT '*.lock'."""
    locks = Path(local_repo) / "locks"
    locks.mkdir(exist_ok=True)
    (locks / "fdc84cb25f511c30bd1c19c7a6559ba62aaca24a8c6dc8391622146ce1c997f7").write_text("lock")
    real_guard()
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    assert result["skipped"] is True
    assert "lock" in result["reason"], result


def test_guard_proc_match_survives_without_proc(data_root, tmp_path, plugin_env,
                                                monkeypatch, real_guard):
    """Non-Linux fallback (Greptile round-1 P1): macOS has no /proc, so the
    guard must identify repos via ps — argv via `ps -axo pid=,command=` and
    the environment via BSD `ps -E` (same-user processes: the scheduler's own
    cycles and the user's other restic stacks). Simulated here by disabling
    the /proc probe and faking the ps outputs."""
    real_guard()  # re-arm the REAL detection over the autouse host isolation
    monkeypatch.setattr(engine, "_proc_available", lambda: False)

    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        if argv[:2] == ["pgrep", "-x"]:
            R.stdout = "4711\n4713\n"
        elif argv[:3] == ["ps", "-axo", "pid=,command="]:
            # NOTE: pid 4711's command line must NOT contain a repository —
            # the engine puts the repo in RESTIC_REPOSITORY, so the positive
            # assertion below must be satisfiable ONLY via the ps -E env
            # output (Greptile round-2 P2). 4713 covers the argv path
            # (--repo) with a different env repo.
            R.stdout = ("  4711 restic backup /home/x\n"
                        "  4713 restic backup --repo /repos/argvonly /home/x\n"
                        "  4712 bash -c sleep 30\n")
        elif argv[:2] == ["ps", "-E"]:
            per_pid = {"4711": "RESTIC_REPOSITORY=/repos/mine",
                       "4713": "RESTIC_REPOSITORY=/repos/foreign"}
            pid = argv[-1]
            R.stdout = (f"  {pid} ttys001  0:00.12 restic\n"
                        + per_pid.get(pid, ""))
        else:
            R.returncode = 1
        return R()

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    assert engine.restic_process_running(["/repos/mine"]) is True  # env only
    assert engine.restic_process_running(["/repos/argvonly"]) is True  # argv only
    assert engine.restic_process_running(["/repos/other"]) is False


def test_cycle_runs_despite_foreign_repo_lock_files(data_root, tmp_path, plugin_env,
                                                    source_tree, real_guard):
    """Lock files in a DIFFERENT repository must never block this config's
    cycle — the drill-round-1 finding, lock-file flavor. Our local repo path
    is deliberately unmounted (absent): the guard must find no locks of ours
    and let the cycle proceed to its leg-level verdicts, all restic-free."""
    foreign = tmp_path / "foreign-repo"
    (foreign / "locks").mkdir(parents=True)
    (foreign / "locks" / "1759000000123456789.lock").write_text("lock")
    real_guard()
    cfg = make_cfg(tmp_path, source_tree,
                   local_repo=str(tmp_path / "not-mounted-repo"))
    result = engine.run_cycle(cfg, prune="skip", secrets=fresh_secrets())
    assert result.get("skipped") is not True, result
    assert result["verdict"] == "fail"  # both legs unmounted/unconfigured


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


def test_failed_prune_retries_within_window(data_root, tmp_path, plugin_env,
                                            source_tree, local_repo,
                                            restic_shim, shim_state, shim_calls,
                                            monkeypatch):
    """Fix round 2 P2 (Greptile PR4): run_cycle recorded hourly maintenance
    as complete even when the prune failed — a transient failure in the
    first cycle of the hour consumed the window, and later auto cycles
    reported success without repeating the failed check. Completion is now
    recorded ONLY when the work succeeded (prune + data check); failed work
    stays eligible for retry inside the window."""
    write_state(shim_state, fake_all=True, forget_rc=1)  # prune fails
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
    monkeypatch.setattr(engine, "_top_of_hour_window", lambda cfg, now=None: True)

    first = engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    assert first["verdict"] == "warn", first  # the failed prune is loud
    assert engine._last_maintenance_epoch() is None, \
        "a failed prune must NOT close the window's slot"
    record = json.loads((Path(engine.plugin_data_dir())
                         / engine.MAINTENANCE_STATE_FILENAME).read_text())
    assert record.get("state") == "failed", record

    # the transient prune failure clears before the next cycle in the window
    write_state(shim_state, forget_rc=0)
    second = engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    forgets = [c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]]
    assert len(forgets) == 2, forgets  # RETRIED — pre-fix code skipped it
    assert "already completed" not in second["detail"].get("prune_note", ""), \
        second["detail"]
    assert second["verdict"] == "ok", second
    assert engine._last_maintenance_epoch() is not None
    record = json.loads((Path(engine.plugin_data_dir())
                         / engine.MAINTENANCE_STATE_FILENAME).read_text())
    assert record.get("state") == "completed", record

    # a COMPLETED pass still blocks repeats in the same window (round-1 P2#9)
    third = engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    forgets = [c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]]
    assert len(forgets) == 2, forgets  # third cycle: skipped, not re-run
    assert "already completed" in third["detail"].get("prune_note", ""), \
        third["detail"]


def test_failed_sample_check_retries_within_window(data_root, tmp_path,
                                                   plugin_env, source_tree,
                                                   local_repo, restic_shim,
                                                   shim_state, shim_calls,
                                                   monkeypatch):
    """Fix round 2 P2: the 2% data check failing also kept the window open —
    completion requires the prune AND the data check to succeed."""
    write_state(shim_state, fake_all=True, check_rc=1)  # data check fails
    cfg = make_cfg(tmp_path, source_tree, local_repo=local_repo)
    monkeypatch.setattr(engine, "_top_of_hour_window", lambda cfg, now=None: True)

    first = engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    assert first["verdict"] == "warn", first
    assert engine._last_maintenance_epoch() is None, \
        "a failed data check must NOT close the window's slot"
    record = json.loads((Path(engine.plugin_data_dir())
                         / engine.MAINTENANCE_STATE_FILENAME).read_text())
    assert record.get("state") == "failed", record

    # the data check heals: the next cycle in the window retries the pass
    write_state(shim_state, check_rc=0)
    second = engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    forgets = [c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]]
    assert len(forgets) == 2, forgets  # RETRIED — pre-fix code skipped it
    assert second["verdict"] == "ok", second
    record = json.loads((Path(engine.plugin_data_dir())
                         / engine.MAINTENANCE_STATE_FILENAME).read_text())
    assert record.get("state") == "completed", record


def test_maintenance_without_local_leg_completes_on_prune(data_root, tmp_path,
                                                          plugin_env,
                                                          source_tree,
                                                          restic_shim,
                                                          shim_state,
                                                          shim_calls,
                                                          monkeypatch):
    """r2-only setup: the 2% sample is a LOCAL-leg check, so the pass
    completes on prune success alone — otherwise every auto cycle in the
    window would re-prune r2 all hour (regressing round-1 P2#9). The record
    still never claims completion unless the prune itself succeeded."""
    write_state(shim_state, fake_all=True)
    cfg = make_cfg(tmp_path, source_tree, r2=True)  # no local repo
    monkeypatch.setattr(engine, "_top_of_hour_window", lambda cfg, now=None: True)

    first = engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    assert first["verdict"] == "ok", first
    assert engine._last_maintenance_epoch() is not None

    repeat = engine.run_cycle(cfg, prune="auto", secrets=fresh_secrets())
    forgets = [c for c in read_calls(shim_calls) if c["argv"][:1] == ["forget"]]
    assert len(forgets) == 1, forgets  # completed pass blocks repeats
    assert "already completed" in repeat["detail"].get("prune_note", ""), \
        repeat["detail"]


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
