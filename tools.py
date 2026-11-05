"""hermes-backup tool handlers — thin, exception-proof JSON wrappers.

Contract (plugin API + catalog rules): every handler has the signature
``(args: dict, **kwargs) -> str``, always returns a JSON string (also on
error), and never raises — the dispatcher additionally signature-inspects the
optional context kwargs it injects (task_id, session_id, ...), which the
``**kwargs`` swallow handles. Handlers contain no business logic: they parse
arguments, call :mod:`engine`, and serialize. Secrets never reach these
strings (engine scrubbing) and are never logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

try:  # package context (Hermes plugin loader, `plugins doctor`)
    from . import engine
    from .schemas import TOOLSET
except ImportError:  # flat context (tests, direct module use)
    import engine
    from schemas import TOOLSET

logger = logging.getLogger("hermes-backup")

VALID_LEGS = ("local", "r2")


def _result(tool: str, **data: Any) -> str:
    return json.dumps({"ok": True, "tool": tool, **data}, ensure_ascii=False)


def tool_error(tool: str, message: str, **extra: Any) -> str:
    """Error envelope — a tool call that cannot proceed still answers JSON."""
    return json.dumps({"ok": False, "tool": tool, "error": str(message), **extra},
                      ensure_ascii=False)


def _guarded(tool: str, fn, args: dict, cfg=None):
    """Never-raise wrapper: any unexpected crash becomes a JSON error."""
    try:
        return fn(args, cfg)
    except Exception as exc:  # noqa: BLE001 — the tool contract forbids raising
        logger.exception("hermes-backup: %s failed unexpectedly", tool)
        return tool_error(tool, f"unexpected failure: {exc}")


def _cfg_from(args: dict, cfg: engine.Config | None = None) -> engine.Config:
    """Config for this call. ``cfg`` (explicit injection, used by tests and
    embedders) wins; otherwise the plugin settings are loaded fresh, with an
    optional ``source_path`` override from the tool args."""
    if cfg is None:
        cfg = engine.load_config()
    override = str(args.get("source_path") or "").strip()
    if override:
        cfg.source_path = override
    return cfg


def _leg_arg(args: dict) -> str:
    leg = str(args.get("leg") or "local").strip().lower()
    return leg if leg in VALID_LEGS else "local"


# ── backup_status ────────────────────────────────────────────────────────────
def _do_status(args, cfg=None) -> str:
    data = engine.build_status(force=bool(args.get("force")), cfg=_cfg_from(args, cfg))
    return _result("backup_status", **data)


# ── backup_now ───────────────────────────────────────────────────────────────
def _do_now(args, cfg=None) -> str:
    prune = str(args.get("prune") or "auto").lower()
    if prune not in ("auto", "force", "skip"):
        return tool_error("backup_now", "prune must be one of auto|force|skip")
    result = engine.run_cycle(_cfg_from(args, cfg), prune=prune)
    return _result("backup_now", **result)


# ── backup_list ──────────────────────────────────────────────────────────────
def _do_list(args, cfg=None) -> str:
    cfg = _cfg_from(args, cfg)
    leg_id = _leg_arg(args)
    leg = engine.get_leg(cfg, leg_id)
    if not leg.configured:
        return tool_error("backup_list", f"{leg_id} leg is not configured — run backup_setup")
    if engine._repo_state(leg) == "unmounted":
        return tool_error("backup_list",
                          f"{leg_id} leg repository path is missing (drive unmounted?)")
    snapshots = engine.list_snapshots(leg)
    return _result("backup_list", leg=leg_id, count=len(snapshots), snapshots=snapshots)


# ── backup_verify ────────────────────────────────────────────────────────────
def _do_verify(args, cfg=None) -> str:
    cfg = _cfg_from(args, cfg)
    leg_id = _leg_arg(args)
    leg = engine.get_leg(cfg, leg_id)
    if not leg.configured:
        return tool_error("backup_verify", f"{leg_id} leg is not configured — run backup_setup")
    sample = args.get("sample_pct")
    if sample is not None:
        try:
            sample = float(sample)
        except (TypeError, ValueError):
            return tool_error("backup_verify", "sample_pct must be a number between 1 and 100")
        if not (0 < sample <= 100):
            return tool_error("backup_verify", "sample_pct must be between 1 and 100")
    result = engine.restic_check(leg, sample_pct=sample)
    data = engine._summarize_check(result)
    out = {"leg": leg_id, "mode": "structural" if sample is None else f"read-data-subset={sample}%",
           "ok": data["ok"], "rc": data["rc"], "detail": data["detail"]}
    if result.timed_out:
        out["error"] = data["detail"]
    return _result("backup_verify", **out)


# ── backup_restore ───────────────────────────────────────────────────────────
def _do_restore(args, cfg=None) -> str:
    path = str(args.get("path") or "").strip()
    if not path:
        return tool_error("backup_restore", "path is required (absolute, inside the source tree)")
    cfg = _cfg_from(args, cfg)
    # path validation first: refuse outside-source paths before touching restic
    try:
        engine.validate_restore_path(path, cfg)
    except ValueError as exc:
        return tool_error("backup_restore", str(exc))
    leg_id = _leg_arg(args)
    result = engine.restore_path(cfg, path=path, leg_id=leg_id,
                                 snapshot_id=str(args.get("snapshot_id") or ""),
                                 target_dir=str(args.get("target_dir") or ""))
    if not result.pop("ok", False):
        return tool_error("backup_restore", result.get("error", "restore failed"),
                          **{k: v for k, v in result.items() if k != "error"})
    result.pop("ok", None)
    return _result("backup_restore", leg=leg_id, **{k: v for k, v in result.items() if k != "leg"})


# ── backup_restore_commit ────────────────────────────────────────────────────
def _do_commit(args, cfg=None) -> str:
    target_dir = str(args.get("target_dir") or "").strip()
    # STRICT approval: only the literal boolean true counts ("fix round 1":
    # bool() accepted any nonempty string — "false" — as approval and then
    # handed the engine a literal True, defeating its own gate).
    confirm = args.get("confirm") is True
    if not target_dir:
        return tool_error("backup_restore_commit",
                          "target_dir (the scratch dir returned by backup_restore) is required")
    if not confirm:
        return tool_error("backup_restore_commit",
                          "refusing to overwrite live files without confirm=true — "
                          "review the restore output with the user first")
    result = engine.restore_commit(_cfg_from(args, cfg), target_dir=target_dir, confirm=True)
    if not result.pop("ok", False):
        return tool_error("backup_restore_commit", result.get("error", "commit failed"),
                          **{k: v for k, v in result.items() if k != "error"})
    return _result("backup_restore_commit", **result)


# ── backup_drill ─────────────────────────────────────────────────────────────
def _do_drill(args, cfg=None) -> str:
    cfg = _cfg_from(args, cfg)
    driver = str(args.get("driver") or "agent")
    result = engine.run_drill(cfg, deep=bool(args.get("deep")),
                              file=str(args.get("file") or ""), driver=driver)
    return _result("backup_drill", **result)


# ── backup_setup ─────────────────────────────────────────────────────────────
def _do_setup(args, cfg=None) -> str:
    cfg = _cfg_from(args, cfg)
    missing = engine.restic_missing()
    if missing:
        return tool_error("backup_setup", missing)
    interval = args.get("interval_minutes")
    if interval is not None:
        try:
            interval = max(1, int(interval))
        except (TypeError, ValueError):
            return tool_error("backup_setup", "interval_minutes must be an integer")
    out: dict[str, Any] = {
        "restic": "installed",
        "config": cfg.as_dict(),
        "platform_scheduler": "launchd" if not engine.shutil.which("systemctl") else "systemd",
    }
    init = str(args.get("init") or "").lower()
    if init:
        if init not in ("local", "r2", "both"):
            return tool_error("backup_setup", "init must be one of local|r2|both")
        targets = ("local", "r2") if init == "both" else (init,)
        out["init"] = {}
        for leg_id in targets:
            out["init"][leg_id] = engine.init_repo(cfg, leg_id)
    out["systemd"] = engine.systemd_unit_text(interval or cfg.interval_minutes)
    out["launchd"] = engine.launchd_plist_text(interval or cfg.interval_minutes, cfg=cfg)
    out["cron_one_liner"] = engine.cron_one_liner(interval or cfg.interval_minutes)
    out["install_note"] = ("Files above are proposals only. Pass install=true (after the "
                           "user approves) to write and enable the systemd user timer; on "
                           "macOS save the launchd plist into LaunchAgents yourself. The "
                           "cron one-liner is the cross-platform alternative.")
    # STRICT approval (mirror of the confirm fix): the systemd install gate
    # takes only the literal boolean true — "install": "true" does not install.
    if args.get("install") is True:
        out["installed"] = engine.install_unit(cfg, interval)
    return _result("backup_setup", **out)


# ── registration table (names MUST match plugin.yaml provides_tools) ────────
HANDLERS = {
    "backup_status": _do_status,
    "backup_now": _do_now,
    "backup_list": _do_list,
    "backup_verify": _do_verify,
    "backup_restore": _do_restore,
    "backup_restore_commit": _do_commit,
    "backup_drill": _do_drill,
    "backup_setup": _do_setup,
}


def handle(tool: str, args: dict, cfg=None, **kwargs) -> str:
    """Dispatch entry used by register(); also the seam tests can call directly.

    ``cfg`` is an explicit config injection (tests/embedders); production
    calls omit it and the handler loads the plugin settings itself.
    """
    fn = HANDLERS.get(tool)
    if fn is None:
        return tool_error(tool, f"unknown tool {tool!r}")
    return _guarded(tool, fn, dict(args or {}), cfg)
