"""Setup/scheduler contracts: strict install gate (fix round 1), launchd plist
environment (fix round 1), referenced password file honoring."""

import json

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
