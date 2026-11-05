"""Registration contract: exactly the 8 manifest tools, skill loop tolerant
of a missing/empty/broken skills dir, handler kwargs-tolerant."""

import json

import __init__ as plugin
import schemas
import tools


class FakeCtx:
    def __init__(self):
        self.tools = []
        self.skills = []

    def register_tool(self, name, toolset, schema, handler, emoji=None, **kw):
        self.tools.append({"name": name, "toolset": toolset, "schema": schema,
                           "handler": handler, "emoji": emoji})

    def register_skill(self, name, path, description="", **kw):
        self.skills.append({"name": name, "path": str(path), "description": description})


def test_registers_exactly_the_manifest_tools(data_root):
    ctx = FakeCtx()
    plugin.register(ctx)
    names = [t["name"] for t in ctx.tools]
    assert names == list(schemas.TOOL_NAMES)
    assert names == ["backup_status", "backup_now", "backup_list", "backup_verify",
                     "backup_restore", "backup_restore_commit", "backup_drill",
                     "backup_setup"]
    assert all(t["toolset"] == schemas.TOOLSET for t in ctx.tools)
    for entry in ctx.tools:
        schema_names = {s["name"] for s in schemas.TOOL_SCHEMAS}
        assert entry["schema"]["name"] in schema_names
        assert callable(entry["handler"])


def test_skill_loop_tolerates_missing_dir(data_root):
    ctx = FakeCtx()
    plugin.register(ctx)  # no skills/ dir in the worktree yet
    assert ctx.skills == []          # loop tolerated the missing dir
    assert len(ctx.tools) == 8       # tools still registered


def test_skill_loop_registers_present_skills(data_root, tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    (skills / "restore").mkdir(parents=True)
    (skills / "restore" / "SKILL.md").write_text(
        "---\nname: restore\ndescription: Restore recipes\n---\n# restore\n")
    (skills / "drill").mkdir()
    (skills / "drill" / "SKILL.md").write_text("# drill\n")
    monkeypatch.setattr(plugin, "__file__", str(tmp_path / "__init__.py"))
    ctx = FakeCtx()
    plugin.register(ctx)
    names = sorted(s["name"] for s in ctx.skills)
    assert names == ["drill", "restore"]
    restore = next(s for s in ctx.skills if s["name"] == "restore")
    assert "Restore recipes" in restore["description"]


def test_skill_loop_survives_one_bad_skill(data_root, tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    (skills / "good").mkdir(parents=True)
    (skills / "good" / "SKILL.md").write_text("# good\n")
    (skills / "bad").mkdir()
    (skills / "bad" / "SKILL.md").write_text("# bad\n")

    class HalfBrokenCtx(FakeCtx):
        def register_skill(self, name, path, description="", **kw):
            if name == "bad":
                raise ValueError("boom")
            super().register_skill(name, path, description, **kw)

    monkeypatch.setattr(plugin, "__file__", str(tmp_path / "__init__.py"))
    ctx = HalfBrokenCtx()
    plugin.register(ctx)
    assert [s["name"] for s in ctx.skills] == ["good"]
    assert len(ctx.tools) == 8  # and the tools are unaffected


def test_registered_handler_accepts_dispatcher_kwargs(data_root):
    """The dispatcher injects optional context kwargs — must be tolerated."""
    ctx = FakeCtx()
    plugin.register(ctx)
    entry = next(t for t in ctx.tools if t["name"] == "backup_status")
    out = entry["handler"]({}, task_id="t1", session_id="s1", user_task="x",
                           parent_agent=None, context=object())
    assert json.loads(out)["ok"] is True


def test_toolset_names_never_collide_with_builtin(data_root):
    assert not any(name.startswith("ha_") for name in schemas.TOOL_NAMES)
    assert len(set(schemas.TOOL_NAMES)) == 8


def test_cycle_entrypoint_importable(data_root):
    """cycle.py resolves engine from its own dir (standalone under systemd)."""
    import subprocess
    import sys
    from conftest import REPO_ROOT
    proc = subprocess.run([sys.executable, "-m", "py_compile", str(REPO_ROOT / "cycle.py")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "cycle.py"), "--prune", "skip"],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(data_root.parent),
             "HERMES_HOME": str(data_root.parent / "hermes-home")})
    # Standalone unconfigured run: a CLEAN verdict line, never a crash or hang.
    # FAIL (nothing configured) or SKIP (the host's own restic stack is
    # running) are both correct outcomes.
    assert out.stdout.startswith("hermes-backup:"), out
    assert out.returncode in (0, 1), out
    assert "Traceback" not in out.stderr
