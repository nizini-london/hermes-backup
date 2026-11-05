"""hermes-backup — status backend for the desktop pane and web dashboard.

Read-only reporting for the widget surfaces: newest snapshot per backup leg,
last cycle verdict, last DR drill verdict, and schedule state. Mounted at
``/api/plugins/hermes-backup/``; the desktop half polls ``GET /status``.

Catalog-contract rules this file holds:

- **Read-only restic** — every restic call here is a *read* and runs with
  ``--no-lock``: a widget poll must never block on (or create) the backup
  cycle's exclusive lock.
- **Secrets (rule 11)** — credentials are resolved only through
  ``agent.secret_scope.get_secret`` (the config_schema ``type: secret``
  route). This module never opens a credential file or auth store directly,
  and never returns or logs a secret value: every string in the response is
  scrubbed against the resolved secrets before it leaves here.
- **Numeric epochs** — all timestamps are epoch seconds as numbers (ints).
  Renderers do the math themselves; restic's ISO strings carry up to 9
  fractional digits, which ``Date.parse`` cannot digest.
- **60 s server-side cache** — chip + pane + any dashboard poller share one
  cache file, so rapid polls never spawn restic subprocesses per tick. The
  refresh is single-flight (concurrent misses share one build instead of
  racing) and the cache entry is stamped when the build FINISHES, so a slow
  build is not born already expired.
- **Leg independence** — each leg is probed inside its own guard; one leg's
  failure or misconfiguration never gates or skips the other.
- **Bounded cold budget** — a cold build must finish inside the desktop
  client's 30 s deadline: each restic probe is capped and both legs share
  one overall budget. A leg that cannot answer in time is reported
  ``stalled`` (partial status), never "missing" — and never mistaken for a
  definitive failure verdict.

The plugin's cycle log lives at ``$HERMES_HOME/plugin-data/hermes-backup/backup.log``
(shared convention with the tools layer — keep both sides identical):
verdict lines look like ``[YYYY-MM-DD HH:MM:SS] OK|FAIL|SKIP ...`` and a
drill appends ``DRILL YYYY-MM-DD PASS|FAIL driver=<who>``.

Stdlib + FastAPI only; the guarded hermes imports degrade to
"unconfigured" answers instead of a 500 when hermes core is not importable
(the real dashboard/gateway process always has it).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter

router = APIRouter()

PLUGIN_ID = "hermes-backup"
CACHE_TTL_SECONDS = 60

# The desktop pane abandons /status after 30 s (REST_TIMEOUT_MS in
# desktop/plugin.js — keep the pair in sync). A cold build therefore works
# to an overall budget: every restic probe is individually capped, both
# legs share one budget, and a leg that misses it is reported ``stalled``
# instead of holding the response past the client deadline — one stalled
# offsite leg must never blank the pane (the local leg's answer still
# ships, the stalled leg ships as "probing/failed").
COLD_BUDGET_SECONDS = 22.0
PROBE_ATTEMPT_TIMEOUT_SECONDS = 8.0
INTER_ATTEMPT_SLEEP_SECONDS = 2.0

# Verdict-to-EOF distance in the cycle log grows without bound (restic
# progress output floods the log between verdicts), so a small fixed tail
# intermittently misses every verdict — read a large 64KB tail instead.
LOG_TAIL_BYTES = 65536

# A verdict must carry a full date: restic progress lines such as
# "[0:04] 100.00%" also bracket their content and must not parse as verdicts.
_VERDICT_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (\w+)\b(.*)$")
_DRILL_RE = re.compile(
    r"^\s*DRILL\s+(\d{4}-\d{2}-\d{2})\s+(\w+)(?:.*\bdriver=(\S+))?", re.IGNORECASE
)

# env names from plugin.yaml's config_schema ``type: secret`` fields.
SECRET_ENV_NAMES = (
    "HERMES_BACKUP_RESTIC_PASSWORD",
    "HERMES_BACKUP_R2_ACCESS_KEY",
    "HERMES_BACKUP_R2_SECRET_KEY",
)

# The systemd user unit the plugin's setup tool installs (convention: the
# unit is named after the plugin). Probed best-effort for the schedule line.
_TIMER_UNIT = f"{PLUGIN_ID}.timer"


# ------------------------------------------------------------- hermes glue --

def _hermes_home() -> Path:
    """The active Hermes home. Guarded import: tests and bare processes get
    the conventional default instead of a hard failure."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def _config_snapshot() -> dict:
    """The profile's config.yaml (read-only; cached by hermes core)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _plugin_settings() -> dict:
    """This plugin's ``plugins.entries.<id>.settings`` subtree — mirrors
    PluginContext.get_config, including the legacy ``config`` fallback.
    Matched by registry key OR manifest name; never raises."""
    cfg = _config_snapshot()
    plugins = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        if key == PLUGIN_ID or entry.get("name") == PLUGIN_ID:
            settings = entry.get("settings") if isinstance(entry.get("settings"), dict) else {}
            legacy = entry.get("config") if isinstance(entry.get("config"), dict) else {}
            merged = dict(legacy)
            merged.update(settings)
            return merged
    return {}


def _secret(name: str) -> Optional[str]:
    """Resolve one credential through the Hermes secret scope (rule 11).
    ``None`` when unresolved — callers degrade to a per-leg failure state,
    never a process error, and never fall back to opening a credential file."""
    try:
        from agent.secret_scope import get_secret

        value = get_secret(name)
        return str(value) if value else None
    except Exception:
        return None


# ------------------------------------------------------------- restic reads --

def _run(argv: list, env: dict, timeout: float = 90) -> subprocess.CompletedProcess:
    """argv-list restic calls only (never shell=True). The timeout is
    per-attempt and chosen by the caller: probe attempts are capped by the
    cold-request budget so a stalled repo cannot hold the widget response
    past the client deadline."""
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)


def _restic_env(
    repo: str,
    password: Optional[str],
    aws_key: Optional[str],
    aws_secret: Optional[str],
) -> dict:
    """Fresh per-call env: inherit the process env, then DROP any inherited
    restic/S3 credential names so a stale or wrong-profile value can never
    ride along, and set only what this call resolved."""
    # Inherit PATH/HOME/etc. for the restic child — this is NOT a credential
    # read (rule 11): every credential NAME below is dropped, and secret
    # VALUES only ever come from agent.secret_scope via the caller.
    env = dict(os.environ)
    for stale in (
        "RESTIC_PASSWORD",
        "RESTIC_PASSWORD_FILE",
        "RESTIC_PASSWORD_COMMAND",
        "RESTIC_REPOSITORY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        env.pop(stale, None)
    env["RESTIC_REPOSITORY"] = repo
    if password:
        env["RESTIC_PASSWORD"] = password
    if repo.startswith("s3:"):
        if aws_key:
            env["AWS_ACCESS_KEY_ID"] = aws_key
        if aws_secret:
            env["AWS_SECRET_ACCESS_KEY"] = aws_secret
    return env


def _iso_to_epoch(value: Any) -> Optional[int]:
    """ISO timestamp (restic emits up to 9 fractional digits) → epoch int.
    Truncate the fraction to microseconds before parsing: ``fromisoformat``
    rejects nanoseconds, and the renderer gets numbers only."""
    if not value:
        return None
    try:
        iso = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6], str(value), count=1)
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:
        return None


def _snapshot_probe(repo: str, env: dict, deadline: Optional[float] = None) -> tuple:
    """``restic snapshots --latest 1 --no-lock --json`` on one repo.

    Returns ``(snapshot|None, sanitized_error|None, stalled)``:
    ``(snap, None, False)``  — repo answered and holds snapshots;
    ``(None, None, False)``  — repo answered, no snapshot yet (not an error);
    ``(None, error, False)`` — unreachable (two attempts, 2 s apart, so a
                               transient hiccup does not flicker the pane red);
    ``(None, error, True)``  — the probe could not FINISH inside the
                               cold-request budget: the leg is reported as
                               ``stalled`` (still probing / too slow), never
                               as a definitive failure (P1 #2 — one slow
                               leg must not blank the pane).

    ``deadline`` is a ``time.monotonic()`` timestamp bounding the whole
    probe; each attempt is capped by the shorter of
    PROBE_ATTEMPT_TIMEOUT_SECONDS and the time remaining, and no attempt
    starts without budget left.
    """

    def remaining() -> Optional[float]:
        if deadline is None:
            return None
        return deadline - time.monotonic()

    last_err = None
    stalled = False
    for attempt in range(2):
        left = remaining()
        if left is not None and left <= 0:
            return (None,
                    last_err or "probe skipped — the status deadline passed before this leg could be checked",
                    True)
        timeout = PROBE_ATTEMPT_TIMEOUT_SECONDS
        if left is not None:
            timeout = min(timeout, max(1.0, left))
        try:
            proc = _run(
                ["restic", "snapshots", "--latest", "1", "--no-lock", "--json"], env, timeout
            )
            if proc.returncode == 0:
                snaps = json.loads(proc.stdout or "[]")
                if isinstance(snaps, list) and snaps:
                    newest = snaps[-1]
                    snap_id = str(newest.get("short_id") or newest.get("id") or "?")
                    return {"id": snap_id, "epoch": _iso_to_epoch(newest.get("time"))}, None, False
                return None, None, False
            stderr = (proc.stderr or "").strip().splitlines()
            last_err = stderr[0] if stderr else f"restic exited {proc.returncode}"
            stalled = False
        except subprocess.TimeoutExpired:
            last_err = f"probe timed out after {timeout:.0f}s — leg did not answer in time"
            stalled = True
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            stalled = False
        if attempt == 0:
            left = remaining()
            if left is not None and left <= 0:
                return None, last_err or "repository unreachable", stalled
            time.sleep(INTER_ATTEMPT_SLEEP_SECONDS)
    return None, last_err or "repository unreachable", stalled


def _scrub(text: str, secret_values: list) -> str:
    """Defense in depth: a secret value that somehow reached an error string
    (a shim echoing stderr, a pasted config) is masked before it leaves."""
    for value in secret_values:
        if value and value in text:
            text = text.replace(value, "***")
    return text


def _scrub_payload(node: Any, secret_values: list) -> Any:
    if isinstance(node, dict):
        return {k: _scrub_payload(v, secret_values) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub_payload(v, secret_values) for v in node]
    if isinstance(node, str):
        return _scrub(node, secret_values)
    return node


def _legs(settings: dict, secrets: dict, deadline: Optional[float] = None) -> list:
    """Per-leg health facts for the configured legs. Independent by
    construction: every leg returns its own facts, whatever the others did.
    ``deadline`` (a ``time.monotonic()`` timestamp) bounds the whole probe
    sequence so one slow leg cannot push the response past the desktop
    client's 30 s deadline (P1 #2) — it comes back ``stalled`` instead."""
    pw = secrets.get("HERMES_BACKUP_RESTIC_PASSWORD")
    aws_key = secrets.get("HERMES_BACKUP_R2_ACCESS_KEY")
    aws_secret = secrets.get("HERMES_BACKUP_R2_SECRET_KEY")
    secret_values = [v for v in (pw, aws_key, aws_secret) if v]

    local_repo = str(settings.get("local_repo") or "").strip()
    endpoint = str(settings.get("r2_endpoint") or "").strip().rstrip("/")
    bucket = str(settings.get("r2_bucket") or "").strip()
    r2_repo = f"s3:{endpoint}/{bucket}" if endpoint and bucket else ""

    out = []
    for leg_name, repo in (("local", local_repo), ("r2", r2_repo)):
        leg = {
            "leg": leg_name,
            "configured": bool(repo),
            "reachable": False,
            "snapshot": None,
            "error": None,
        }
        if not repo:
            leg["error"] = "not configured — run backup_setup"
            out.append(leg)
            continue
        if shutil.which("restic") is None:
            leg["error"] = "restic is not installed — run backup_setup for the install command"
            out.append(leg)
            continue
        if not pw:
            leg["error"] = "repository password not configured (secret HERMES_BACKUP_RESTIC_PASSWORD)"
            out.append(leg)
            continue
        env = _restic_env(
            repo, pw,
            aws_key if repo.startswith("s3:") else None,
            aws_secret if repo.startswith("s3:") else None,
        )
        snap, err, stalled = _snapshot_probe(repo, env, deadline)
        if err is None:
            # The repo ANSWERED — an empty repo is reachable, not an error
            # (P2 #5): "no snapshots yet" is a true state, not "down".
            leg["reachable"] = True
            if snap:
                leg["snapshot"] = snap
        else:
            leg["error"] = _scrub(err, secret_values)
            if stalled:
                # Probe could not finish inside the budget (P1 #2): ship it
                # as partial status, not as a definitive failure verdict.
                leg["stalled"] = True
        out.append(leg)
    return out


# -------------------------------------------------------------- log parsing --

def _log_path() -> Path:
    return _hermes_home() / "plugin-data" / PLUGIN_ID / "backup.log"


def _tail_log(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _last_run(path: Path) -> Optional[dict]:
    """Most recent cycle verdict from the log tail. Scans the whole (already
    64KB-capped) tail — the verdict-to-EOF gap grows every cycle."""
    hits = []
    for line in _tail_log(path).splitlines():
        m = _VERDICT_RE.match(line.strip())
        if m:
            hits.append(m.groups())
    if not hits:
        return None
    ts, verdict, detail = hits[-1]
    return {"epoch": _iso_to_epoch(ts), "status": verdict, "detail": (detail or "").strip()}


def _last_drill(path: Path) -> Optional[dict]:
    """Most recent ``DRILL <date> <verdict> driver=<who>`` line."""
    hits = []
    for line in _tail_log(path).splitlines():
        m = _DRILL_RE.search(line)
        if m:
            hits.append(m.groups())
    if not hits:
        return None
    date, verdict, driver = hits[-1]
    epoch = None
    try:
        epoch = int(datetime.strptime(date, "%Y-%m-%d").timestamp())
    except Exception:
        pass
    return {"epoch": epoch, "date": date, "verdict": verdict.upper(), "driver": driver or None}


# ----------------------------------------------------------------- schedule --

def _systemd_timer() -> Optional[dict]:
    """Linux opt-in timer installed by backup_setup (``hermes-backup.timer``).
    Returns None when systemd is absent or the unit does not exist."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", _TIMER_UNIT,
             "--property=NextElapseUSecRealtime", "--property=ActiveState"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        out = proc.stdout or ""
        active = "ActiveState=active" in out
        match = re.search(r"NextElapseUSecRealtime=(\d+)", out)
        if active and not match:
            # OnCalendar timers sometimes report the next elapse only in
            # list-timers; fall back to the next 5-minute boundary.
            return {"kind": "systemd", "active": True,
                    "next_run_epoch": int((int(time.time()) // 300 + 1) * 300)}
        return {"kind": "systemd", "active": active,
                "next_run_epoch": int(int(match.group(1)) / 1e6) if (match and active) else None}
    except Exception:
        return None


_CRON_HINTS = ("backup_now", PLUGIN_ID)


def _cron_job() -> Optional[dict]:
    """Cross-platform alternative: an enabled Hermes cron job whose fields
    reference this plugin's tools. Earliest next run wins; best-effort."""
    try:
        raw = json.loads((_hermes_home() / "cron" / "jobs.json").read_text("utf-8"))
        jobs = raw if isinstance(raw, list) else raw.get("jobs", [])
    except Exception:
        return None
    best = None
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        blob = json.dumps(job, default=str).lower()
        if not any(hint in blob for hint in _CRON_HINTS):
            continue
        if not job.get("enabled", True) or job.get("state") in {"paused", "completed"}:
            continue
        nxt = _iso_to_epoch(job.get("next_run_at"))
        if nxt is None:
            continue
        if best is None or nxt < best["next_run_epoch"]:
            best = {"kind": "cron", "active": True, "next_run_epoch": nxt}
    return best


def _schedule() -> dict:
    systemd = _systemd_timer()
    if systemd:
        return systemd
    cron = _cron_job()
    if cron:
        return cron
    return {"kind": "none", "active": False, "next_run_epoch": None}


# -------------------------------------------------------------------- route --

def _cache_path() -> Path:
    return _hermes_home() / "plugin-data" / PLUGIN_ID / "widget-status.json"


# Single-flight refresh (P2 #4): every cold cache miss used to run its own
# _build_payload() AND write the same fixed .tmp path — duplicate restic
# subprocess bursts and a write race on one temp file. One build at a time;
# waiters then serve the winner's freshly-published entry.
_build_lock = threading.Lock()


def _build_payload(deadline: Optional[float] = None) -> dict:
    settings = _plugin_settings()
    secrets = {name: _secret(name) for name in SECRET_ENV_NAMES}
    try:
        interval = float(settings.get("interval_minutes") or 5)
    except (TypeError, ValueError):
        interval = 5.0
    payload = {
        "plugin": PLUGIN_ID,
        "interval_minutes": interval if interval > 0 else 5.0,
        "legs": _legs(settings, secrets, deadline),
        "last_run": _last_run(_log_path()),
        "last_drill": _last_drill(_log_path()),
        "schedule": _schedule(),
        "now": int(time.time()),
    }
    # Nothing may carry a secret value out of this module — scrub everything.
    return _scrub_payload(payload, [v for v in secrets.values() if v])


def _read_cache(cache: Path, now: float) -> Optional[dict]:
    """The cached payload when it is inside the TTL, else None (any read or
    parse problem — including a torn file — degrades to a rebuild)."""
    try:
        cached = json.loads(cache.read_text("utf-8"))
        if now - float(cached.get("ts", 0)) < CACHE_TTL_SECONDS:
            data = cached.get("data")
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def _publish_cache(cache: Path, payload: dict) -> None:
    """Atomically cache the finished payload. Two correctness rules:

    - the entry is stamped with the COMPLETION time (P2 #3) — a slow
      _build_payload used to be recorded with its start time and born
      already expired, so every later poll rebuilt during outages;
    - each write uses its OWN temp file (P2 #4) — a shared fixed .tmp path
      raced between writers and could replace the cache with torn content.
    """
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "data": payload}
        fd, tmp_name = tempfile.mkstemp(
            dir=str(cache.parent), prefix=cache.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(record))
            os.replace(tmp_name, cache)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        pass


@router.get("/status")
def status():
    """``GET /api/plugins/hermes-backup/status`` — the widget's one data call.

    Plain ``def`` on purpose: with the default ``plugins.isolation:
    in_process`` handlers share the gateway's event loop, and this handler
    blocks on restic subprocesses — a plain def keeps that work in FastAPI's
    worker threadpool instead of stalling every other dashboard request.
    """
    cache = _cache_path()
    fresh = _read_cache(cache, time.time())
    if fresh is not None:
        return fresh
    with _build_lock:
        # Another caller may have finished the refresh while we waited on
        # the lock — serve its fresh entry instead of rebuilding (P2 #4).
        fresh = _read_cache(cache, time.time())
        if fresh is not None:
            return fresh
        # Cold build inside a strict overall budget (P1 #2): the desktop
        # client stops waiting at 30 s, so the whole build — both legs'
        # probes included — is bounded here. A slow leg answers "stalled"
        # (partial status); it never holds the response past the deadline.
        deadline = time.monotonic() + COLD_BUDGET_SECONDS
        payload = _build_payload(deadline)
        _publish_cache(cache, payload)
    return payload
