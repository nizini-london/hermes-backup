"""Shared fixtures: run the engine outside a Hermes process on system python.

- sys.path: repo root, so ``import engine`` works under ``python -m pytest tests/``.
- fake ``plugins.plugin_storage``: engine.plugin_data_dir() resolves to a
  per-test tmp dir instead of the host's Hermes home (the real import needs
  a running Hermes).
- ``plugin_env``: restic password + cache isolation via the same env names
  the engine's rule-11 helper falls back to in standalone mode.
- ``restic_shim``: a fake restic for s3: repos on PATH (transparent proxy to
  the real /usr/bin/restic for local repos), recording every invocation.
"""

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The default exclude globs include "*/cache/*"; pytest's default tmp root
# under a Hermes scratch dir contains a "cache" path component, which would
# exclude the ENTIRE test source tree. Point pytest's tmp root at a clean
# path (PYTEST_DEBUG_TEMPROOT is honored when the tmp factory is built).
if "cache" in os.environ.get("PYTEST_DEBUG_TEMPROOT", "").lower() or \
        "cache" in os.environ.get("TMPDIR", "").lower():
    _base = "/tmp/hermes-backup-pytest"
    os.makedirs(_base, exist_ok=True)
    os.environ["PYTEST_DEBUG_TEMPROOT"] = _base

TEST_PASSWORD = "test-restic-password-0123456789"


@pytest.fixture(autouse=True)
def no_host_restic(monkeypatch):
    """This host may run its own production restic stack on a five-minute
    timer — tests must be isolated from it. The pgrep guard is neutralized;
    tests that verify the guard itself re-arm the REAL check via the
    ``real_pgrep`` fixture."""
    import engine
    original = engine.restic_process_running
    monkeypatch.setattr(engine, "restic_process_running", lambda: False)
    yield original


@pytest.fixture()
def real_pgrep(no_host_restic, monkeypatch):
    """Re-arm the REAL ``pgrep restic`` detection (override the autouse
    isolation) for tests that exercise the running-cycle refusal path."""
    import engine

    def arm():
        monkeypatch.setattr(engine, "restic_process_running", no_host_restic)

    return arm


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    """Engine plugin data dir → tmp; import engine AFTER the fake is armed."""
    fake_root = tmp_path / "plugin-data"
    fake_storage = type(sys)("plugins.plugin_storage")
    fake_storage.plugin_data_dir = lambda name: _mkdir(fake_root / name)
    fake_storage.plugin_db = lambda name, filename="data.db": _mkdir(fake_root / name) / filename
    fake_plugins = type(sys)("plugins")
    fake_plugins.plugin_storage = fake_storage
    monkeypatch.setitem(sys.modules, "plugins", fake_plugins)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", fake_storage)
    for key in ("HERMES_BACKUP_RESTIC_PASSWORD", "HERMES_BACKUP_R2_ACCESS_KEY",
                "HERMES_BACKUP_R2_SECRET_KEY", "HERMES_BACKUP_SOURCE",
                "HERMES_BACKUP_LOCAL_REPO", "HERMES_BACKUP_R2_ENDPOINT",
                "HERMES_BACKUP_R2_BUCKET", "HERMES_BACKUP_INTERVAL_MINUTES"):
        monkeypatch.delenv(key, raising=False)
    import engine
    yield fake_root
    # Do NOT pop engine from sys.modules here: test modules keep a module-level
    # `import engine` binding, so a re-import would desynchronize the object the
    # autouse pgrep patch hits from the one the tests call. The module is
    # stateless between tests (per-call plugin_data_dir + secrets).


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def plugin_env(monkeypatch, tmp_path):
    """Standalone secret resolution + isolated restic cache."""
    monkeypatch.setenv("HERMES_BACKUP_RESTIC_PASSWORD", TEST_PASSWORD)
    monkeypatch.setenv("HERMES_BACKUP_R2_ACCESS_KEY", "test-access-key")
    monkeypatch.setenv("HERMES_BACKUP_R2_SECRET_KEY", "test-secret-key-0123")
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "restic-cache"))
    return {"password": TEST_PASSWORD}


@pytest.fixture()
def source_tree(tmp_path):
    """A Hermes-home-like source tree: config file + hot sqlite DBs + caches."""
    source = tmp_path / "hermes-home"
    (source / "profiles" / "default").mkdir(parents=True)
    (source / ".cache").mkdir()
    (source / "config.yaml").write_text("display:\n  skin: default\n")
    (source / "notes.md").write_text("small real file for dump tests\n")
    _mk_sqlite(source / "state.db")
    _mk_sqlite(source / "profiles" / "default" / "state.db")
    (source / ".cache" / "junk.bin").write_bytes(b"x" * 2048)
    return source


def _mk_sqlite(path: Path) -> None:
    """Create a real WAL-mode sqlite database at ``path``."""
    import sqlite3
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS t (k TEXT, v TEXT)")
    db.execute("INSERT INTO t VALUES ('probe', 'hello')")
    db.commit()
    db.close()


@pytest.fixture()
def local_repo(tmp_path, plugin_env):
    """A REAL tiny restic repo (restic init costs ~a second)."""
    repo = tmp_path / "backup-repo"
    repo.mkdir()
    env = dict(os.environ)
    env["RESTIC_PASSWORD"] = plugin_env["password"]
    env["RESTIC_CACHE_DIR"] = os.environ["RESTIC_CACHE_DIR"]
    import subprocess
    proc = subprocess.run(["restic", "--repo", str(repo), "init"],
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return repo


@pytest.fixture()
def shim_state(tmp_path):
    """State file the fake restic reads its behavior from."""
    return tmp_path / "shim-state.json"


@pytest.fixture()
def shim_calls(tmp_path):
    """Every fake-restic invocation lands here: {argv, repo, rc} lines."""
    return tmp_path / "shim-calls.jsonl"


@pytest.fixture()
def restic_shim(tmp_path, monkeypatch, shim_state, shim_calls):
    """Install the shim as `restic` on PATH. s3: repos are faked from
    ``shim_state``; everything else proxies the real restic binary."""
    shim_dir = tmp_path / "shim-bin"
    shim_dir.mkdir()
    shim = shim_dir / "restic"
    shim.write_text(SHIM_SCRIPT)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HERMES_RESTIC_SHIM_STATE", str(shim_state))
    monkeypatch.setenv("HERMES_RESTIC_SHIM_CALLS", str(shim_calls))
    shim_state.write_text(json.dumps({}))
    return shim


def read_calls(shim_calls: Path) -> list[dict]:
    if not shim_calls.exists():
        return []
    return [json.loads(line) for line in shim_calls.read_text().splitlines() if line.strip()]


def write_state(shim_state: Path, **state) -> None:
    current = json.loads(shim_state.read_text()) if shim_state.exists() else {}
    current.update(state)
    shim_state.write_text(json.dumps(current))


SHIM_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    # restic shim: fake s3: repos from a state file; real repos proxy through.
    import json, os, subprocess, sys
    args = sys.argv[1:]
    state_p = os.environ.get("HERMES_RESTIC_SHIM_STATE")
    try:
        state = json.load(open(state_p)) if state_p and os.path.exists(state_p) else {}
    except (OSError, ValueError):
        state = {}
    calls_p = os.environ.get("HERMES_RESTIC_SHIM_CALLS")
    repo = os.environ.get("RESTIC_REPOSITORY", "")

    def record(rc):
        if calls_p:
            with open(calls_p, "a") as f:
                f.write(json.dumps({"argv": args, "repo": repo, "rc": rc}) + "\\n")

    if not state.get("fake_all") and repo and not repo.startswith("s3:"):
        real = os.environ.get("RESTIC_REAL", "/usr/bin/restic")
        proc = subprocess.run([real, *args], env=dict(os.environ),
                              capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        record(proc.returncode)
        sys.exit(proc.returncode)

    cmd = args[0] if args else ""
    rc, out = 0, ""
    if cmd == "snapshots":
        out = json.dumps(state.get("snapshots", []))
    elif cmd == "check":
        rc = int(state.get("check_rc", 0))
        out = state.get("check_out", "using cache\\nno errors were found")
    elif cmd == "backup":
        rc = int(state.get("backup_rc", 0))
        out = state.get("backup_out",
                        json.dumps({"message_type": "summary", "snapshot_id": "f00dface",
                                    "total_bytes_processed": 1234}))
    elif cmd == "dump":
        stored = args[2] if len(args) > 2 else ""
        per_name = state.get("dump_by_name") or {}
        dump = per_name.get(stored) or state.get("dump_path")
        if dump and os.path.exists(dump):
            sys.stdout.buffer.write(open(dump, "rb").read())
            record(0)
            sys.exit(0)
        rc, out = 1, state.get("dump_error", "error: file does not exist in snapshot")
    elif cmd == "stats":
        out = json.dumps({"total_size": state.get("total_size", 4096)})
    elif cmd == "forget":
        rc = int(state.get("forget_rc", 0))
    elif cmd == "init":
        rc = int(state.get("init_rc", 0))
    else:
        rc = int(state.get("other_rc", 0))
    record(rc)
    if out:
        sys.stdout.write(out + ("" if out.endswith("\\n") else "\\n"))
    sys.exit(rc)
""")


def snapshot_fixture(epoch_time: str = "2026-11-05T09:00:00.123456789Z",
                     short_id: str = "abcdef12") -> list[dict]:
    """A fake `restic snapshots --json` payload with 9-digit fractional time."""
    return [{
        "id": short_id + "0" * 32, "short_id": short_id, "time": epoch_time,
        "paths": ["/home/test/hermes-home"], "hostname": "test", "tags": [],
        "summary": {"total_bytes_processed": 1234},
    }]
