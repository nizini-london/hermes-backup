"""Setup/scheduler contracts: strict install gate (fix round 1), launchd plist
environment (fix round 1), referenced password file honoring, optional
systemd EnvironmentFile (QA S2)."""

import json
import os

import engine
import tools


def make_cfg(tmp_path, source=None, local_repo=None):
    return engine.Config(source_path=str(source) if source else "",
                         local_repo=str(local_repo) if local_repo else "")


def test_setup_install_requires_literal_true(data_root, tmp_path, monkeypatch):
    """Fix-round-1 P1 mirror: ``install`` is an approval gate — only the
    literal boolean true installs. ``"install": "true"`` must NOT install."""
    which_real = engine.shutil.which
    monkeypatch.setattr(engine.shutil, "which",
                        lambda name, *a, **k: None if name == "systemctl"
                        else which_real(name, *a, **k))  # no real unit writes anywhere
    cfg = make_cfg(tmp_path, source=tmp_path / "home", local_repo=tmp_path / "repo")
    out = tools.handle("backup_setup", {"install": "true"}, cfg=cfg)
    parsed = json.loads(out)
    assert parsed["ok"] is True, out
    assert "installed" not in parsed, parsed  # old code: truthy string installed


# ── fix round 1, P1#7: the launchd job gets its settings via the plist ───────

def test_launchd_plist_carries_environment_variables(data_root, tmp_path,
                                                     monkeypatch):
    """P1#7: the generated launchd job starts cycle.py with NO environment —
    every cycle would fail (source unresolvable, no secrets). The plist now
    carries EnvironmentVariables (HERMES_HOME, source, repos, cadence) plus a
    RESTIC_PASSWORD_FILE reference — never an inlined secret value."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fake-hermes"))
    cfg = make_cfg(tmp_path, source=tmp_path / "hermes-home",
                   local_repo=tmp_path / "backup-repo")
    out = engine.launchd_plist_text(7, cfg=cfg)
    import plistlib
    plist = plistlib.loads(out["plist"].encode())
    assert plist["Label"] == engine.LAUNCHD_LABEL
    env = plist.get("EnvironmentVariables") or {}
    assert env.get("HERMES_HOME") == str(tmp_path / "fake-hermes")
    assert env.get("HERMES_BACKUP_SOURCE") == str(tmp_path / "hermes-home")
    assert env.get("HERMES_BACKUP_LOCAL_REPO") == str(tmp_path / "backup-repo")
    assert env.get("HERMES_BACKUP_INTERVAL_MINUTES") == "7"
    pw_file = env.get("RESTIC_PASSWORD_FILE") or ""
    assert pw_file.endswith("restic-password") and pw_file.startswith("/")
    # secrets are never inlined into the plist
    from conftest import TEST_PASSWORD
    assert TEST_PASSWORD not in out["plist"]
    assert "HERMES_BACKUP_R2_ACCESS_KEY" not in out["plist"]
    assert "HERMES_BACKUP_R2_SECRET_KEY" not in out["plist"]
    assert out["setup_steps"] and any("chmod 600" in s for s in out["setup_steps"])


def test_launchd_plist_degrades_loudly_when_config_incomplete(data_root, tmp_path,
                                                              monkeypatch):
    """P1#7: with nothing configured the plist generation says so loudly —
    the job would refuse every cycle otherwise."""
    import sys
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    cfg = make_cfg(tmp_path)  # no source, no repo
    out = engine.launchd_plist_text(5, cfg=cfg)
    warnings = out.get("warnings") or []
    assert any("source" in w.lower() for w in warnings), warnings
    assert any("repository" in w.lower() for w in warnings), warnings


def test_launchd_plist_warns_offsite_keys_not_carried(data_root, tmp_path,
                                                      monkeypatch):
    """P1#7: the offsite S3 keys cannot ride the plist (secrets are never
    inlined) — setup says so loudly when the r2 leg is configured."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fake-hermes"))
    cfg = engine.Config(source_path=str(tmp_path / "home"),
                        r2_endpoint="https://acct.r2.cloudflarestorage.com",
                        r2_bucket="bucket")
    out = engine.launchd_plist_text(5, cfg=cfg)
    warnings = out.get("warnings") or []
    assert any("S3" in w or "offsite" in w.lower() for w in warnings), warnings


def test_secrets_load_honors_referenced_password_file(data_root, tmp_path,
                                                      monkeypatch):
    """P1#7: RESTIC_PASSWORD_FILE (a PATH, never a value) feeds the repo
    password when the config_schema secret is absent; the env secret wins
    when both exist. This is the launchd contract."""
    from conftest import TEST_PASSWORD
    pw_file = tmp_path / "restic-password"
    pw_file.write_text(TEST_PASSWORD + "\n")
    monkeypatch.delenv("HERMES_BACKUP_RESTIC_PASSWORD", raising=False)
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)
    assert engine.Secrets.load().restic_password == ""
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(pw_file))
    assert engine.Secrets.load().restic_password == TEST_PASSWORD
    monkeypatch.setenv("HERMES_BACKUP_RESTIC_PASSWORD", "env-wins-password-123")
    assert engine.Secrets.load().restic_password == "env-wins-password-123"


# ── QA S2: the systemd unit must not brick on a missing EnvironmentFile ──────
def _fake_systemctl(monkeypatch, fake_home):
    """A no-op systemctl on PATH, so install_unit exercises its real flow
    without touching the host's user units."""
    bin_dir = fake_home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_install_unit_tolerates_missing_env_file(data_root, tmp_path, monkeypatch):
    """QA S2: the unit used a MANDATORY EnvironmentFile and enabled --now —
    every zero-touch install failed at unit load until the user hand-created
    the file. The unit now uses the optional '-EnvironmentFile=' form and the
    install reports the absence loudly instead of bricking."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))  # Path.home() AND expanduser
    _fake_systemctl(monkeypatch, fake_home)
    cfg = make_cfg(tmp_path, source=tmp_path / "home", local_repo=tmp_path / "repo")
    out = engine.install_unit(cfg, 5)
    assert out["ok"] is True, out
    service = (fake_home / ".config/systemd/user/hermes-backup.service").read_text()
    assert "EnvironmentFile=-%h/.config/hermes-backup.env" in service, service
    assert out["env_file_missing"] is True
    assert "does not exist yet" in out["note"], out
    assert "FAIL" in out["note"], out  # the per-cycle failure is named, not hidden


def test_install_unit_env_file_present_note(data_root, tmp_path, monkeypatch):
    """QA S2 mirror: with the env file present, install reports the normal
    maintain-it note and env_file_missing=False."""
    fake_home = tmp_path / "fakehome"
    (fake_home / ".config").mkdir(parents=True)
    (fake_home / ".config" / "hermes-backup.env").write_text(
        "HERMES_BACKUP_SOURCE=/x\n")
    monkeypatch.setenv("HOME", str(fake_home))  # Path.home() AND expanduser
    _fake_systemctl(monkeypatch, fake_home)
    cfg = make_cfg(tmp_path, source=tmp_path / "home", local_repo=tmp_path / "repo")
    out = engine.install_unit(cfg, 5)
    assert out["ok"] is True, out
    assert out["env_file_missing"] is False
    assert "maintain" in out["note"], out


def test_systemd_unit_text_optional_env_file(data_root):
    """QA S2: the PROPOSED unit text (what the user reviews before install)
    also carries the optional prefix — the template cannot brick either."""
    units = engine.systemd_unit_text(5)
    assert "EnvironmentFile=-%h/.config/hermes-backup.env" in units["service"]
