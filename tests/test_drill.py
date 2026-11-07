"""DR drill: sequential steps, dump+byte-compare from EACH leg, PASS/FAIL
per step, DRILL log line, deep flag, pgrep refusal while a cycle runs."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import engine
import tools
from conftest import read_calls, write_state, snapshot_fixture


def align_candidate_mtimes(source_tree):
    """QA-N2 ordering companion: candidates are mtime-eligible when their
    mtime predates the snapshot cutoff. These tests' shim snapshots carry a
    FIXED past epoch (2026-11-05T09:00Z) while the fixture files are touched
    now — model the scenario they describe (files existed AT snapshot time)
    by stamping the candidate files before that epoch. Content is untouched."""
    cutoff = engine._parse_snapshot_time(snapshot_fixture()[0]["time"])
    assert cutoff is not None, "fixture snapshot time must parse"
    for rel in ("config.yaml", "notes.md", ".cache/junk.bin"):
        candidate = source_tree / rel
        if candidate.is_file():
            os.utime(candidate, (cutoff - 60, cutoff - 60))


def make_cfg(tmp_path, source, local_repo=None, r2=False):
    return engine.Config(source_path=str(source),
                         local_repo=str(local_repo) if local_repo else "",
                         r2_endpoint="https://acct.r2.cloudflarestorage.com" if r2 else "",
                         r2_bucket="bucket" if r2 else "")


def test_drill_pass_both_legs(data_root, tmp_path, plugin_env, source_tree,
                              local_repo, restic_shim, shim_state, shim_calls):
    """Local leg REAL (proxy), r2 leg FAKE: both dump-test PASS → drill PASS."""
    recent = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(time.time() - 10))
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture(epoch_time=recent),
                dump_path=str(source_tree / "config.yaml"))
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo), r2=True)
    out = tools.handle("backup_drill", {}, cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is True, out
    assert parsed["verdict"] == "PASS", parsed
    steps = {s["step"]: s for s in parsed["steps"]}
    assert steps["snapshot_age_both_legs"]["verdict"] == "PASS"
    assert steps["structural_checks_both_legs"]["verdict"] == "PASS"
    dump_step = steps["dump_and_byte_compare"]
    assert dump_step["verdict"] == "PASS", dump_step
    assert set(dump_step["attempt"]["legs"]) == {"local", "r2"}
    assert dump_step["attempt"]["matches_live"] is True
    assert dump_step["attempt"]["cross_leg_match"] is True
    drill = engine.last_drill_verdict()
    assert drill is not None and drill["verdict"] == "PASS" and drill["driver"] == "agent"


def test_drill_file_pick_mismatch_is_not_a_backup_failure(data_root, tmp_path,
                                                          plugin_env, source_tree,
                                                          restic_shim, shim_state):
    """Dumps succeed from the leg but the candidate differs from live →
    file_pick_mismatch, NOT fail: one stale file never proves a broken backup."""
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture(),
                dump_path=str(source_tree / "notes.md"))  # ≠ live config.yaml
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    parsed = json.loads(tools.handle("backup_drill", {"file": str(source_tree / "config.yaml")}, cfg=cfg))
    assert parsed["verdict"] == "PASS", parsed  # no hard-failing step
    dump_step = next(s for s in parsed["steps"] if s["step"] == "dump_and_byte_compare")
    assert dump_step["verdict"] == "file_pick_mismatch"
    assert "NOT evidence of a broken backup" in dump_step["detail"]
    assert engine.last_drill_verdict()["verdict"] == "PASS"


def test_drill_repicks_after_mismatch_and_passes(data_root, tmp_path, plugin_env,
                                                 source_tree, restic_shim, shim_state):
    """config.yaml differs from live AND junk.bin is absent from the snapshot
    → re-pick lands on a stable file → PASS, with both attempt kinds recorded."""
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture(),
                dump_by_name={
                    str(source_tree / "config.yaml"): str(source_tree / "notes.md"),
                    str(source_tree / "notes.md"): str(source_tree / "notes.md"),
                })  # junk.bin: not in dump_by_name → absent → re-pick
    align_candidate_mtimes(source_tree)
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    parsed = json.loads(tools.handle("backup_drill", {}, cfg=cfg))
    dump_step = next(s for s in parsed["steps"] if s["step"] == "dump_and_byte_compare")
    assert parsed["verdict"] == "PASS", parsed
    assert dump_step["verdict"] == "PASS"
    assert dump_step["file"] == str(source_tree / "notes.md")
    assert len(dump_step["attempts"]) == 2
    assert dump_step["attempts"][0]["verdict"] == "file_pick_mismatch"  # live mismatch
    assert "not present in the snapshot" in dump_step["attempts"][1]["reason"]  # absent


def test_drill_absent_candidates_exhausted_is_not_a_failure(data_root, tmp_path,
                                                            plugin_env, source_tree,
                                                            restic_shim, shim_state):
    """Every candidate absent from the snapshot (e.g. all excluded) →
    file_pick_mismatch, NOT fail: nothing was proven broken."""
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture())  # no fixtures
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    parsed = json.loads(tools.handle("backup_drill", {}, cfg=cfg))
    dump_step = next(s for s in parsed["steps"] if s["step"] == "dump_and_byte_compare")
    assert dump_step["verdict"] == "file_pick_mismatch"
    assert parsed["verdict"] == "PASS"


def test_drill_fails_when_leg_cannot_serve_the_file(data_root, tmp_path, plugin_env,
                                                    source_tree, restic_shim, shim_state):
    """A ready leg that hits a READ/integrity error IS a real backup problem →
    FAIL (distinct from an absent candidate, which just re-picks)."""
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture(),
                dump_error="integrity error: pack is damaged")
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    parsed = json.loads(tools.handle("backup_drill", {}, cfg=cfg))
    assert parsed["verdict"] == "FAIL"
    dump_step = next(s for s in parsed["steps"] if s["step"] == "dump_and_byte_compare")
    assert dump_step["verdict"] == "FAIL"
    assert "backup problem" in dump_step["detail"]
    assert engine.last_drill_verdict()["verdict"] == "FAIL"


def test_drill_binary_file_dump_is_byte_exact(data_root, tmp_path, plugin_env,
                                              source_tree, local_repo):
    """Fix round 1 P1: dump output is captured as BYTES and hashed raw. A
    binary candidate (NUL bytes, invalid-UTF-8 sequences, CRLF) must not
    crash the drill with UnicodeDecodeError (text-mode capture) nor normalize
    CRLF into a false live-mismatch."""
    payload = bytes(range(256)) * 64 + b"\r\nCR/LF probe\r\n\x00tail\x00"
    (source_tree / "blob.bin").write_bytes(payload)
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    result = engine.run_cycle(cfg, prune="skip",
                              secrets=engine.Secrets(restic_password=engine.get_secret(
                                  "HERMES_BACKUP_RESTIC_PASSWORD")))
    assert result["verdict"] == "ok", result
    parsed = json.loads(tools.handle("backup_drill",
                                     {"file": str(source_tree / "blob.bin")}, cfg=cfg))
    assert parsed["verdict"] == "PASS", parsed  # old code: step FAILED on decode error
    dump_step = next(s for s in parsed["steps"] if s["step"] == "dump_and_byte_compare")
    assert dump_step["verdict"] == "PASS", dump_step
    attempt = dump_step["attempt"]
    assert attempt["cross_leg_match"] is True
    assert attempt["matches_live"] is True, attempt
    import hashlib
    assert attempt["live_sha256"] == hashlib.sha256(payload).hexdigest()
    assert attempt["legs"]["local"]["size_bytes"] == len(payload)


def test_drill_partial_leg_supply_never_passes(data_root, tmp_path, plugin_env,
                                               source_tree, local_repo,
                                               restic_shim, shim_state, shim_calls):
    """Fix round 1 P1: a cross-leg match requires EVERY ready leg to supply
    the candidate. One leg dumping the file while the other cannot even serve
    it must NOT pass — that masks the broken leg (old code passed on the
    half-verified file); the drill re-picks instead."""
    # real local leg: a REAL snapshot containing all candidates
    cfg_real = make_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    result = engine.run_cycle(cfg_real, prune="skip",
                              secrets=engine.Secrets(restic_password=engine.get_secret(
                                  "HERMES_BACKUP_RESTIC_PASSWORD")))
    assert result["verdict"] == "ok", result
    # shimmed r2 leg (no fake_all: the local repo proxies to REAL restic):
    # it serves ONLY notes.md — config.yaml is absent from it
    write_state(shim_state, snapshots=snapshot_fixture(),
                dump_by_name={str(source_tree / "notes.md"): str(source_tree / "notes.md")})
    align_candidate_mtimes(source_tree)
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo), r2=True)
    parsed = json.loads(tools.handle("backup_drill", {}, cfg=cfg))
    dump_step = next(s for s in parsed["steps"] if s["step"] == "dump_and_byte_compare")
    assert dump_step["verdict"] == "PASS", dump_step
    assert dump_step["file"] == str(source_tree / "notes.md")  # old code: passed on config.yaml
    first = dump_step["attempts"][0]
    assert first["file"] == str(source_tree / "config.yaml")
    assert first["verdict"] == "file_pick_mismatch"
    assert first["cross_leg_match"] is False, first  # old code: True (single-leg sha)
    assert set(first["legs_supplied"]) == {"local"}, first
    assert first["legs"]["r2"].get("absent") is True


def test_drill_skips_cleanly_when_nothing_configured(data_root, tmp_path, plugin_env,
                                                     source_tree):
    """Unconfigured repo set → per-leg SKIP verdicts, not crashes, no FAIL."""
    cfg = make_cfg(tmp_path, source_tree)
    parsed = json.loads(tools.handle("backup_drill", {}, cfg=cfg))
    assert parsed["verdict"] == "PASS"  # SKIP is not a failure
    ages = next(s for s in parsed["steps"] if s["step"] == "snapshot_age_both_legs")
    assert ages["verdict"] == "SKIP"
    for leg in ages["ages"].values():
        assert leg["verdict"] == "SKIP" and "not configured" in leg["reason"]
    checks = next(s for s in parsed["steps"] if s["step"] == "structural_checks_both_legs")
    assert checks["verdict"] == "SKIP"
    dump = next(s for s in parsed["steps"] if s["step"] == "dump_and_byte_compare")
    assert dump["verdict"] == "SKIP"


def test_drill_fails_when_structural_check_fails(data_root, tmp_path, plugin_env,
                                                 source_tree, restic_shim, shim_state):
    write_state(shim_state, snapshots=snapshot_fixture(),
                dump_path=str(source_tree / "config.yaml"), check_rc=1)
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    parsed = json.loads(tools.handle("backup_drill", {}, cfg=cfg))
    assert parsed["verdict"] == "FAIL"
    checks = next(s for s in parsed["steps"] if s["step"] == "structural_checks_both_legs")
    assert checks["verdict"] == "FAIL"


def test_drill_fails_when_no_snapshots(data_root, tmp_path, plugin_env, source_tree,
                                       local_repo, restic_shim, shim_state):
    write_state(shim_state, fake_all=True, snapshots=[],
                dump_path=str(source_tree / "config.yaml"))
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo), r2=True)
    parsed = json.loads(tools.handle("backup_drill", {}, cfg=cfg))
    assert parsed["verdict"] == "FAIL"
    ages = next(s for s in parsed["steps"] if s["step"] == "snapshot_age_both_legs")
    assert ages["verdict"] == "FAIL"
    assert ages["ages"]["local"]["verdict"] == "FAIL"
    assert "no snapshots" in ages["ages"]["local"]["reason"]


def test_drill_deep_flag_reads_data(data_root, tmp_path, plugin_env, source_tree,
                                    local_repo, restic_shim, shim_state, shim_calls):
    """deep=true → the local leg check carries --read-data + a loud warning."""
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture(),
                dump_path=str(source_tree / "config.yaml"))
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    parsed = json.loads(tools.handle("backup_drill", {"deep": True}, cfg=cfg))
    assert parsed["deep"] is True
    assert "many minutes" in parsed["deep_warning"]
    local_checks = [c for c in read_calls(shim_calls) if c["argv"][:1] == ["check"]
                    and not c["repo"].startswith("s3:")]
    assert local_checks and "--read-data" in local_checks[0]["argv"]


def test_drill_no_deep_by_default(data_root, tmp_path, plugin_env, source_tree,
                                  local_repo, restic_shim, shim_state, shim_calls):
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture(),
                dump_path=str(source_tree / "config.yaml"))
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    parsed = json.loads(tools.handle("backup_drill", {}, cfg=cfg))
    assert parsed["deep"] is False and parsed["deep_warning"] is None
    local_checks = [c for c in read_calls(shim_calls) if c["argv"][:1] == ["check"]
                    and not c["repo"].startswith("s3:")]
    assert local_checks and "--read-data" not in local_checks[0]["argv"]


def test_drill_refuses_steps_while_restic_runs(data_root, tmp_path, plugin_env,
                                               source_tree, local_repo, real_pgrep):
    """pgrep restic → every step refuses (never launch into a locked repo)."""
    fake_bin = engine.plugin_data_dir() / "fake-restic-drill"
    fake_bin.mkdir(parents=True, exist_ok=True)
    proc_path = fake_bin / "restic"
    shutil.copy2("/bin/sleep", proc_path)
    process = subprocess.Popen([str(proc_path), "30"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.3)
        real_pgrep()
        cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo))
        parsed = json.loads(tools.handle("backup_drill", {"file": str(source_tree / "config.yaml")}, cfg=cfg))
        assert parsed["verdict"] == "FAIL"
        assert all("restic cycle is running" in s["detail"] for s in parsed["steps"])
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_drill_steps_strictly_sequential(data_root, tmp_path, plugin_env, source_tree,
                                         local_repo, restic_shim, shim_state, shim_calls):
    """Order must be: ages → checks → dumps (the ops-reference sequence)."""
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture(),
                dump_path=str(source_tree / "config.yaml"))
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    tools.handle("backup_drill", {}, cfg=cfg)
    order = [c["argv"][0] for c in read_calls(shim_calls) if c["argv"]]
    snaps = [i for i, cmd in enumerate(order) if cmd == "snapshots"]
    checks = [i for i, cmd in enumerate(order) if cmd == "check"]
    dumps = [i for i, cmd in enumerate(order) if cmd == "dump"]
    assert snaps and checks and dumps
    assert max(snaps) < min(checks), "all age queries before any check"
    assert max(checks) < min(dumps), "all checks before any dump"


# ── QA N2: documented mtime-eligible ordering + vanished-file tolerance ──────
def test_drill_candidates_mtime_eligible_first(data_root, tmp_path):
    """QA N2: the docstring promised 'mtime-eligible candidates first' but the
    implementation returned plain sorted-rglob order — a file modified AFTER
    the snapshot (a guaranteed dump-vs-live mismatch, a wasted attempt) could
    occupy an attempt slot before a stable file. With a cutoff, eligible
    candidates come first; config.yaml stays preferred within the bucket."""
    import os
    source = tmp_path / "src"
    source.mkdir()
    (source / "config.yaml").write_text("k: v\n")    # stable + preferred
    (source / "aaa-churn.bin").write_text("x" * 16)  # modified after snapshot
    (source / "zzz-stable.md").write_text("y" * 16)  # stable
    cutoff = int(time.time()) - 3600  # snapshot 1h ago
    os.utime(source / "config.yaml", (cutoff - 10, cutoff - 10))
    os.utime(source / "zzz-stable.md", (cutoff - 10, cutoff - 10))
    future = int(time.time()) + 60
    os.utime(source / "aaa-churn.bin", (future, future))
    out = engine._drill_candidates(source, cutoff=cutoff)
    assert out[0] == "config.yaml", out
    assert out.index("zzz-stable.md") < out.index("aaa-churn.bin"), out


def test_drill_candidates_without_cutoff_keep_historical_order(data_root, tmp_path):
    """No snapshot epochs yet (cutoff None) → the historical order stands:
    config.yaml preferred, everything else in sorted walk order."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "config.yaml").write_text("k: v\n")
    (source / "b.txt").write_text("x")
    (source / "a.txt").write_text("y")
    out = engine._drill_candidates(source)
    assert out == ["config.yaml", "a.txt", "b.txt"]


def test_drill_candidates_survives_vanished_file(data_root, tmp_path, monkeypatch):
    """QA N2: a file vanishing between the rglob walk and its stat() raised
    OSError out of _drill_candidates → the whole drill step crashed into FAIL
    instead of just skipping that candidate. The walk now treats a vanished
    entry as 'skip and re-pick', never a backup problem."""
    import pathlib
    source = tmp_path / "src"
    source.mkdir()
    (source / "config.yaml").write_text("k: v\n")
    (source / "ghost.txt").write_text("x" * 10)
    real_stat = pathlib.Path.stat

    def flaky_stat(self, **kwargs):
        if self.name == "ghost.txt":
            raise FileNotFoundError("vanished mid-walk")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", flaky_stat)
    out = engine._drill_candidates(source)
    assert "ghost.txt" not in out, out
    assert "config.yaml" in out, out
