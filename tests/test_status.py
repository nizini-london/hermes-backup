"""Status: unconfigured guidance, 60s cache, numeric epochs, RPO, timer."""

import json
import time

import engine
from conftest import snapshot_fixture, write_state


def make_cfg(tmp_path, source, **kw):
    return engine.Config(source_path=str(source), **kw)


def configured_cfg(tmp_path, source, local_repo=None):
    """Configured local leg: real repo when provided, else an existing dir."""
    repo = str(local_repo) if local_repo else str(tmp_path / "status-local-repo")
    if local_repo is None:
        import pathlib
        pathlib.Path(repo).mkdir(parents=True, exist_ok=True)
    return engine.Config(source_path=str(source), local_repo=repo)


def test_unconfigured_gives_backup_setup_guidance(data_root, tmp_path, source_tree, plugin_env):
    data = engine.build_status(cfg=make_cfg(tmp_path, source_tree), secrets=engine.Secrets())
    assert data["configured"] is False
    assert "backup_setup" in data["guidance"]
    for leg in data["legs"].values():
        assert leg["enabled"] is False
        assert "backup_setup" in leg["note"]


def test_configured_legs_numeric_epochs_and_rpo(data_root, tmp_path, source_tree,
                                                local_repo, restic_shim, shim_state):
    """All timestamps numeric (the widget bug: Date.parse can't take 9-digit
    fractional seconds — the renderer needs ints)."""
    recent = time.strftime("%Y-%m-%dT%H:%M:%S.123456789Z", time.gmtime(time.time() - 30))
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture(epoch_time=recent),
                total_size=8888)
    cfg = configured_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    data = engine.build_status(cfg=cfg, secrets=engine.Secrets(restic_password="pw"))
    leg = data["legs"]["local"]
    assert leg["enabled"] is True
    snap = leg["last_snapshot"]
    assert isinstance(snap["time_epoch"], int)
    assert 0 < snap["age_seconds"] < 120
    assert snap["age_seconds"] == int(data["now_epoch"] - snap["time_epoch"])
    assert leg["repo_size_bytes"] == 8888
    assert data["rpo_interval_minutes"] == 5
    assert leg["rpo"]["behind_schedule"] is False


def test_rpo_behind_schedule_flag(data_root, tmp_path, source_tree, restic_shim, shim_state):
    """Age > 2x interval → 'snapshots behind schedule'."""
    old = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(time.time() - 3600))
    write_state(shim_state, fake_all=True, snapshots=snapshot_fixture(epoch_time=old))
    cfg = configured_cfg(tmp_path, source_tree)
    data = engine.build_status(cfg=cfg, secrets=engine.Secrets(restic_password="pw"))
    leg = data["legs"]["local"]
    assert leg["rpo"]["behind_schedule"] is True
    assert leg["last_snapshot"]["age_seconds"] > 2 * 5 * 60


def test_status_cache_60s(data_root, tmp_path, source_tree, local_repo,
                          restic_shim, shim_calls):
    """Second status call inside 60s serves the cache: NO new restic invocations."""
    cfg = configured_cfg(tmp_path, source_tree, local_repo=str(local_repo))
    first = engine.build_status(cfg=cfg, secrets=engine.Secrets(restic_password="pw"))
    calls_after_first = len(engine_build_calls(shim_calls))
    second = engine.build_status(cfg=cfg, secrets=engine.Secrets(restic_password="pw"))
    assert len(engine_build_calls(shim_calls)) == calls_after_first, "cache miss!"
    assert second["now_epoch"] == first["now_epoch"]  # served from cache
    assert second["cache_age_seconds"] >= 0
    forced = engine.build_status(cfg=cfg, secrets=engine.Secrets(restic_password="pw"), force=True)
    assert forced["now_epoch"] >= first["now_epoch"]
    assert len(engine_build_calls(shim_calls)) > calls_after_first  # force re-queried


def engine_build_calls(shim_calls):
    from conftest import read_calls
    return [c for c in read_calls(shim_calls)
            if c["argv"] and c["argv"][0] in ("snapshots", "stats", "check")]


def test_status_cache_is_atomic_and_readable(data_root, tmp_path, source_tree, plugin_env):
    import json as _json
    cache = engine.plugin_data_dir() / engine.STATUS_CACHE_FILENAME
    data = engine.build_status(cfg=make_cfg(tmp_path, source_tree), secrets=engine.Secrets())
    on_disk = _json.loads(cache.read_text())
    assert on_disk["data"]["configured"] is False  # matches what was served
    assert "status-cache.json.tmp" not in [p.name for p in cache.parent.iterdir()]


def test_timer_state_shape(data_root):
    state = engine.timer_state()
    assert set(state) >= {"kind", "active", "next_epoch"}
    assert state["kind"] in (None, "systemd", "launchd")


def test_unit_texts_and_cron_liner(data_root, tmp_path):
    units = engine.systemd_unit_text(5)
    assert "OnCalendar=*:0/5" in units["timer"]
    # QA S2 (fix round 2): optional '-EnvironmentFile=' — a missing env file
    # must not fail the unit at load; the install reports the absence loudly.
    assert "EnvironmentFile=-%h/.config/hermes-backup.env" in units["service"]
    assert "cycle.py" in units["service"]
    assert "EnvironmentFile" not in units["timer"]
    assert engine._on_calendar(7).startswith("*-*-* *:0/7")
    plist = engine.launchd_plist_text(5)
    assert plist["label"] in plist["plist"]
    assert "<integer>300</integer>" in plist["plist"]
    liner = engine.cron_one_liner(5)
    assert liner.startswith("hermes cron create '5m'") and "backup_now" in liner
