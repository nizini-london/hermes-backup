"""Restore safety: scratch-first, path containment, restic-verified bytes,
informational matches_live (never a gate), confirm-gated commit with
pre-commit safety copies. Fix round 1 adds: custom target_dir confinement
to the plugin scratch root, literal-true confirm."""

import json

import engine
import tools
from conftest import read_calls, snapshot_fixture, write_state


def make_cfg(tmp_path, source, local_repo=None, r2=False):
    return engine.Config(source_path=str(source),
                         local_repo=str(local_repo) if local_repo else "",
                         r2_endpoint="https://acct.r2.cloudflarestorage.com" if r2 else "",
                         r2_bucket="bucket" if r2 else "")


def cycle_ok(cfg):
    result = engine.run_cycle(cfg, prune="skip",
                              secrets=engine.Secrets(restic_password=engine.get_secret(
                                  "HERMES_BACKUP_RESTIC_PASSWORD")))
    assert result["verdict"] == "ok", result


def test_refuses_paths_outside_source(data_root, tmp_path, source_tree):
    cfg = make_cfg(tmp_path, source_tree)
    for bad in ("/etc/passwd", str(tmp_path / "outside" / "file"), "~/notes"):
        out = tools.handle("backup_restore", {"path": bad}, cfg=cfg)
        parsed = json.loads(out)
        assert parsed["ok"] is False, (bad, out)
        assert "outside the backup source tree" in parsed["error"] or \
               "absolute" in parsed["error"], (bad, out)


def test_refuses_relative_and_empty_paths(data_root, tmp_path, source_tree):
    cfg = make_cfg(tmp_path, source_tree)
    out = tools.handle("backup_restore", {"path": "config.yaml"}, cfg=cfg)
    assert json.loads(out)["ok"] is False
    out = tools.handle("backup_restore", {})
    assert json.loads(out)["ok"] is False


def test_real_restore_to_scratch_with_sha_verify(data_root, tmp_path, plugin_env,
                                                 source_tree, local_repo):
    """Real restic repo: snapshot → restore (--verify) → scratch, sha vs live."""
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    cycle_ok(cfg)
    live_file = source_tree / "config.yaml"
    out = tools.handle("backup_restore", {"path": str(live_file)}, cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is True, out
    assert parsed["restic_verified"] is True
    assert parsed["files"], "expected the restored file listed"
    entry = parsed["files"][0]
    assert entry["live_path"] == str(live_file)
    assert entry["matches_live"] is True
    assert entry["sha256"] == entry["live_sha256"]
    assert parsed["summary"]["matching_live"] == 1
    scratch = parsed["scratch_dir"]
    assert scratch.startswith(str(engine.plugin_data_dir()))
    # the restored bytes equal the live bytes
    import hashlib
    from pathlib import Path
    restored = Path(scratch) / str(live_file).lstrip("/")
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == entry["sha256"]


def test_restore_detects_drifted_live_file(data_root, tmp_path, plugin_env,
                                           source_tree, local_repo):
    """Snapshot, then MODIFY the live file: restored copy differs — expected,
    informational, never a failure."""
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    cycle_ok(cfg)
    (source_tree / "config.yaml").write_text("drifted content\n")
    out = tools.handle("backup_restore", {"path": str(source_tree / "config.yaml")}, cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert parsed["files"][0]["matches_live"] is False
    assert parsed["files"][0]["live_sha256"] != parsed["files"][0]["sha256"]
    # the steer: a differing live file is EXPECTED (the restore is the repair)
    assert "the restore is the repair" in parsed["files"][0]["reason"]
    assert parsed["summary"]["differing_live"] == 1


def test_restore_deleted_live_file_is_informational(data_root, tmp_path, plugin_env,
                                                    source_tree, local_repo):
    """A file DELETED since the snapshot: restored fine, matches_live null +
    reason — and confirm is the only gate for committing it back."""
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    cycle_ok(cfg)
    (source_tree / "notes.md").unlink()  # delete the live file
    out = tools.handle("backup_restore", {"path": str(source_tree / "notes.md")}, cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is True, out
    entry = parsed["files"][0]
    assert entry["matches_live"] is None
    assert "deleted" in entry["reason"]
    assert parsed["summary"]["missing_live"] == 1
    out = tools.handle("backup_restore_commit",
                       {"target_dir": parsed["scratch_dir"], "confirm": True}, cfg=cfg)
    committed = json.loads(out)
    assert committed["ok"] is True, out
    assert (source_tree / "notes.md").exists()  # file back from the snapshot


def test_commit_refuses_without_confirm(data_root, tmp_path, source_tree):
    cfg = make_cfg(tmp_path, source_tree)
    out = tools.handle("backup_restore_commit", {"target_dir": str(engine.plugin_data_dir())}, cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert "confirm" in parsed["error"]


def test_commit_confirm_must_be_literal_true(data_root, tmp_path, source_tree):
    """Fix-round-1 P1: ``confirm`` accepts ONLY the literal boolean true.

    ``bool(args.get("confirm"))`` turned any nonempty string ("false", "no")
    into approval and then handed the engine a literal True, so the engine's
    own gate could never catch it. Truthy-but-not-true values are refused."""
    cfg = make_cfg(tmp_path, source_tree)
    target = engine.plugin_data_dir() / "gate-probe"  # must never be touched
    for bad in ("true", "false", "yes", 1):
        out = tools.handle("backup_restore_commit",
                           {"target_dir": str(target), "confirm": bad}, cfg=cfg)
        parsed = json.loads(out)
        assert parsed["ok"] is False, (bad, out)
        # the TOOL gate must be the one refusing (never the engine's dir check)
        assert "refusing to overwrite live files" in parsed["error"], (bad, out)
        assert not target.exists(), (bad, out)


def test_commit_refuses_target_outside_plugin_data(data_root, tmp_path, source_tree):
    cfg = make_cfg(tmp_path, source_tree)
    scratch = tmp_path / "somewhere-else"
    scratch.mkdir()
    out = tools.handle("backup_restore_commit", {"target_dir": str(scratch), "confirm": True}, cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert "plugin data dir" in parsed["error"]


def test_commit_replaces_live_with_safety_copy(data_root, tmp_path, plugin_env,
                                               source_tree, local_repo):
    """Full roundtrip: backup → snapshot → drift live → restore scratch →
    commit → live file back to snapshot content; pre-commit safety copy
    preserved the DRIFTED content first."""
    cfg = make_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    cycle_ok(cfg)
    live = source_tree / "config.yaml"
    drifted = "drifted content\n"
    live.write_text(drifted)
    out = tools.handle("backup_restore", {"path": str(live)}, cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is True and parsed["files"][0]["matches_live"] is False

    out = tools.handle("backup_restore_commit",
                       {"target_dir": parsed["scratch_dir"], "confirm": True}, cfg=cfg)
    committed = json.loads(out)
    assert committed["ok"] is True, out
    assert live.read_text() == "display:\n  skin: default\n"  # snapshot content back
    safety = committed["safety_snapshot"]["files"]
    assert safety and safety[0]["path"] == str(live)
    from pathlib import Path
    assert Path(safety[0]["safety_copy"]).read_text() == drifted  # reversible commit
    assert committed["committed"][0]["verified"] is True


def test_commit_skips_outside_source(data_root, tmp_path, source_tree):
    cfg = make_cfg(tmp_path, source_tree)
    scratch = engine.plugin_data_dir() / "restore-scratch" / "manual"
    (scratch / "etc").mkdir(parents=True)
    (scratch / "etc" / "passwd").write_text("root:x:0:0\n")
    out = tools.handle("backup_restore_commit", {"target_dir": str(scratch), "confirm": True}, cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is False  # nothing committable: the only file is outside source
    assert parsed["skipped"] and parsed["skipped"][0]["reason"] == "outside source tree"


# ── fix round 1, P1#1: custom restore targets confined to the scratch root ───

def _shimmed_r2(shim_state):
    """Fake-ready r2 leg (shim answers every command with rc 0 — restic is
    never really launched, so a red run cannot write anywhere)."""
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture())


def test_restore_refuses_target_outside_scratch_root(data_root, tmp_path,
                                                     plugin_env, source_tree,
                                                     restic_shim, shim_state,
                                                     shim_calls):
    """P1#1: target_dir='/' forwarded to 'restic restore --target' wrote files
    at their original LIVE paths, bypassing confirm + the pre-commit safety
    copy. Any target outside <plugin-data>/scratch/ is refused BEFORE restic."""
    _shimmed_r2(shim_state)
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    for bad in ("/", "/etc", str(tmp_path / "elsewhere-restore")):
        out = tools.handle("backup_restore",
                           {"path": str(source_tree / "config.yaml"), "target_dir": bad,
                            "leg": "r2"},
                           cfg=cfg)
        parsed = json.loads(out)
        assert parsed["ok"] is False, (bad, out)
        assert "scratch" in parsed["error"], (bad, out)
    assert not [c for c in read_calls(shim_calls) if c["argv"][:1] == ["restore"]], \
        "restic restore must never be launched with an unconfined target"


def test_restore_refuses_symlink_escape_target(data_root, tmp_path, plugin_env,
                                               source_tree, restic_shim,
                                               shim_state, shim_calls):
    """P1#1: a symlink inside the scratch root pointing OUTSIDE is an escape —
    refused; restic is never launched."""
    _shimmed_r2(shim_state)
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    link = engine.plugin_data_dir() / "scratch" / "escape"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(tmp_path, target_is_directory=True)  # EXISTING outside dir
    out = tools.handle("backup_restore",
                       {"path": str(source_tree / "config.yaml"), "target_dir": str(link),
                        "leg": "r2"},
                       cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is False, out
    assert "scratch" in parsed["error"], out
    assert not [c for c in read_calls(shim_calls) if c["argv"][:1] == ["restore"]]


def test_restore_refuses_nonempty_existing_target(data_root, tmp_path, plugin_env,
                                                  source_tree, restic_shim,
                                                  shim_state, shim_calls):
    """P1#1: custom targets must be EMPTY or new — a dirty dir would let restic
    merge restored bytes into unknown content."""
    _shimmed_r2(shim_state)
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    taken = engine.plugin_data_dir() / "scratch" / "taken"
    (taken / "junk").mkdir(parents=True)
    (taken / "junk" / "f").write_text("x")
    out = tools.handle("backup_restore",
                       {"path": str(source_tree / "config.yaml"), "target_dir": str(taken),
                        "leg": "r2"},
                       cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is False, out
    assert "empty" in parsed["error"], out
    assert not [c for c in read_calls(shim_calls) if c["argv"][:1] == ["restore"]]


def test_restore_accepts_custom_target_under_scratch_root(data_root, tmp_path,
                                                          plugin_env, source_tree,
                                                          restic_shim, shim_state,
                                                          shim_calls):
    """P1#1 positive: an empty, new dir inside <plugin-data>/scratch/ is a
    valid custom target and is passed through to --target verbatim."""
    _shimmed_r2(shim_state)
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    target = engine.plugin_data_dir() / "scratch" / "my-restore"
    out = tools.handle("backup_restore",
                       {"path": str(source_tree / "config.yaml"), "target_dir": str(target),
                        "leg": "r2"},
                       cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is True, out
    assert parsed["scratch_dir"] == str(target)
    restores = [c for c in read_calls(shim_calls) if c["argv"][:1] == ["restore"]]
    assert len(restores) == 1, restores
    argv = restores[0]["argv"]
    assert argv[argv.index("--target") + 1] == str(target)
    assert "--verify" in argv


def test_restore_default_target_is_fresh_dir_under_scratch_root(data_root,
                                                                tmp_path,
                                                                plugin_env,
                                                                source_tree,
                                                                restic_shim,
                                                                shim_state):
    """P1#1: the default (no target_dir) remains a fresh scratch dir — now
    under the same <plugin-data>/scratch/ root."""
    _shimmed_r2(shim_state)
    cfg = make_cfg(tmp_path, source_tree, r2=True)
    out = tools.handle("backup_restore", {"path": str(source_tree / "config.yaml"),
                                          "leg": "r2"}, cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is True, out
    assert parsed["scratch_dir"].startswith(str(engine.plugin_data_dir() / "scratch"))
