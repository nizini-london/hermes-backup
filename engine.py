"""hermes-backup engine — restic wrapper and backup/restore/drill primitives.

Ports a proven production backup-stack discipline (see BRIEF.md engine port
table) to a plugin engine:

- every restic invocation is an argv list through ``subprocess.run`` (never
  ``shell=True``) with a fresh per-call env built from the process env plus
  secrets fetched through ``agent.secret_scope.get_secret`` (catalog rule 11);
- ``--no-lock`` on every read-only restic call so programmatic readers never
  queue behind a running cycle's exclusive lock;
- per-leg independence: a down local leg never gates the offsite leg;
- restic exit code 3 (source files vanished mid-read) is tolerated;
- prune + the 2% data sample are throttled to the first cycle after the top of
  the hour via ``prune="auto"``; a failed or skipped pass records "failed"
  and is retried by the next cycle in the window — only a pass where the
  prune AND the data check succeeded records "completed" and closes the slot;
- hot SQLite databases are shadow-copied INSIDE the backup source tree
  (``<source>/.db-shadow/``) before a cycle and cleaned up after it;
- single-instance cycles via a non-blocking lockfile in the plugin data dir;
- cycle log under the plugin data dir, verdict lines parseable by
  ``LOG_VERDICT_RE`` over a 64KB tail (verdict-to-EOF distance grows without
  bound, so a fixed small tail intermittently misses every verdict).

Secrets are never logged, never returned, and never read from credential
files — the single exception is the standalone scheduler's
``RESTIC_PASSWORD_FILE`` (the launchd plist references the file; the engine
reads only that path, only in ``Secrets.load``, and never logs the value).
Any text that leaves this module passes through ``scrub()``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from xml.sax.saxutils import escape as xml_escape
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes-backup")

PLUGIN_ID = "hermes-backup"

# Cycle log + status cache live under the plugin data dir (never under the
# install dir, which `hermes plugins remove/update` replaces).
LOG_FILENAME = "backup.log"
STATUS_CACHE_FILENAME = "status-cache.json"
CACHE_TTL_SECONDS = 60
LOG_TAIL_BYTES = 65536          # verdict regex window — see module docstring
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_KEEP_BYTES = 1024 * 1024

# One password protects both legs (one repo set = one password; losing it
# loses the backups — this is README/FAQ material, not just config text).
SECRET_ENV_NAMES = {
    "restic_password": "HERMES_BACKUP_RESTIC_PASSWORD",
    "r2_access_key": "HERMES_BACKUP_R2_ACCESS_KEY",
    "r2_secret_key": "HERMES_BACKUP_R2_SECRET_KEY",
}

# Verdict lines look like "[2026-11-05 10:00:00] OK (local=1 r2=1) …".
# Requiring the full bracketed date keeps restic progress output such as
# "[0:04] 100.00%" from ever parsing as a verdict (widget-proven regex).
LOG_VERDICT_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (\w+)(.*)$")
LOG_DRILL_RE = re.compile(r"^DRILL (\d{4}-\d{2}-\d{2}) (PASS|FAIL)(?: driver=(\S+))?\s*$")

RESTIC_RC_VANISHED = 3  # source files vanished mid-read — tolerated by design

# Per-command subprocess timeouts (seconds). A legitimate cycle takes
# minutes; unattended runs must still fail cleanly rather than hang forever.
TIMEOUTS = {
    "snapshots": 120,
    "stats": 180,
    "init": 120,
    "check": 600,
    "check_read_data": 3600,
    "backup": 1800,
    "restore": 900,
    "dump": 300,
    "forget": 1800,
}

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SHADOW_DIRNAME = ".db-shadow"
# Depth counted in edges from the source root: state.db at 0, and the
# per-profile homes at profiles/<name>/state.db = 3. Deeper than that are
# third-party trees (venvs, node_modules) whose DBs are library data, not
# agent state — and the hermes-agent subtree is excluded from snapshots by
# default anyway, so shadow-copying it would be pure waste.
SHADOW_SCAN_DEPTH = 3
SHADOW_SKIP_DIRS = frozenset({
    SHADOW_DIRNAME, "hermes-agent", "node_modules", "venv", ".venv",
    "__pycache__", ".git", "site-packages", "browser_screenshots",
    "image_cache", "audio_cache", "checkpoints", "snapshots", ".cache",
})

RESTIC_BINARY = "restic"


def plugin_data_dir() -> Path:
    """`<hermes home>/plugin-data/hermes-backup/`, created on demand.

    Under Hermes: the core helper (resolves the active profile on every call,
    profile-safe). Standalone callers (``cycle.py`` under systemd, pytest)
    lack the core module: fall back to ``$HERMES_HOME``/``~/.hermes`` so the
    scheduler entry point keeps the same layout it had inside Hermes.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir as _pdd

        return _pdd(PLUGIN_ID)
    except Exception:
        pass
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    root = home / "plugin-data" / PLUGIN_ID
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── secrets (catalog rule 11) ────────────────────────────────────────────────
def get_secret(name: str, default: str = "") -> str:
    """Resolve a credential through Hermes's secret route.

    Runtime: ``agent.secret_scope.get_secret`` — profile-scoped, reads the
    ``config_schema`` secret fields by their declared env name. Standalone
    callers (the systemd ``cycle.py``, pytest) run outside Hermes, where the
    real helper itself falls through to the process env; this fallback keeps
    the same semantics. It is the ONLY credential read in the plugin and it
    lives here: no handler, script or test reads credential files or env
    vars directly.
    """
    try:
        from agent.secret_scope import get_secret as _get

        return _get(name) or ""
    except ImportError:
        # Standalone mode (cycle.py under systemd, tests): the env name is
        # injected by the init system / test fixture, not read from a file.
        return os.environ.get(name) or default


@dataclass
class Secrets:
    restic_password: str = ""
    r2_access_key: str = ""
    r2_secret_key: str = ""

    @classmethod
    def load(cls) -> "Secrets":
        data = {k: get_secret(v) for k, v in SECRET_ENV_NAMES.items()}
        if not data["restic_password"]:
            # launchd contract (fix round 1, P1#7): the scheduler plist cannot
            # inline secret values, so it references a password FILE via
            # RESTIC_PASSWORD_FILE (restic's own native variable). This is the
            # engine's only file-based credential read: the path comes from
            # the init system's env, the value is never logged nor returned.
            pw_file = os.environ.get("RESTIC_PASSWORD_FILE") or ""
            if pw_file:
                try:
                    data["restic_password"] = Path(pw_file).read_text().strip()
                except OSError:
                    pass  # leg state reports "no-password" — loud, not silent
        return cls(**data)


# ── scrubbing: secret values / names never leave the engine ─────────────────
_REDACTED = "<redacted>"
_SECRET_NAME_RE = re.compile(r"(HERMES_BACKUP_[A-Z_]+|RESTIC_PASSWORD|AWS_(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY))"
                             r"(=|\s*[:=]\s*)[^\s\"']+")
VALUE_SCRUB_MIN_LEN = 6  # shorter values cannot be scrubbed safely by substring


def scrub(text: str, secrets: Optional[Secrets] = None) -> str:
    """Best-effort redaction of credential material from any outgoing text."""
    if not text:
        return text
    out = _SECRET_NAME_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", str(text))
    if secrets is not None:
        for value in (secrets.restic_password, secrets.r2_access_key, secrets.r2_secret_key):
            if value and len(value) >= VALUE_SCRUB_MIN_LEN:
                out = out.replace(value, _REDACTED)
    return out


# ── configuration ────────────────────────────────────────────────────────────
@dataclass
class Config:
    source_path: str = ""
    local_repo: str = ""
    r2_endpoint: str = ""
    r2_bucket: str = ""
    interval_minutes: int = 5
    local_keep_within_hours: int = 48
    local_keep_last: int = 12
    r2_keep_within_hours: int = 72
    r2_keep_last: int = 48
    excludes: list[str] = field(default_factory=lambda: [
        "*/cache/*", "*/image_cache/*", "*/audio_cache/*",
        "*/browser_screenshots/*", "*/snapshots/*", "*/checkpoints/*",
        ".hermes/hermes-agent/*", ".cache/*",
    ])

    @property
    def source(self) -> Optional[Path]:
        path = (self.source_path or "").strip()
        if path:
            return Path(os.path.expanduser(path)).resolve()
        # Default: the active profile's Hermes home.
        try:
            from hermes_constants import get_hermes_home
            return Path(get_hermes_home()).resolve()
        except Exception:
            home = os.environ.get("HERMES_HOME")  # standalone (systemd) injection
            return Path(home).resolve() if home else None

    def leg(self, leg_id: str) -> Optional[str]:
        """Repository URL for a leg, or None when the leg is not configured."""
        if leg_id == "local":
            repo = (self.local_repo or "").strip()
            return os.path.expanduser(repo) if repo else None
        if leg_id == "r2":
            endpoint = (self.r2_endpoint or "").strip().rstrip("/")
            bucket = (self.r2_bucket or "").strip()
            if endpoint and bucket:
                return f"s3:{endpoint}/{bucket}"
        return None

    def retention(self, leg_id: str) -> tuple[int, int]:
        if leg_id == "local":
            return self.local_keep_within_hours, self.local_keep_last
        return self.r2_keep_within_hours, self.r2_keep_last

    def as_dict(self, with_source: bool = True) -> dict:
        d = {
            "local_repo": self.local_repo, "r2_endpoint": self.r2_endpoint,
            "r2_bucket": self.r2_bucket, "interval_minutes": self.interval_minutes,
            "local_keep_within_hours": self.local_keep_within_hours,
            "local_keep_last": self.local_keep_last,
            "r2_keep_within_hours": self.r2_keep_within_hours,
            "r2_keep_last": self.r2_keep_last, "excludes": list(self.excludes),
            "legs": {"local": bool(self.leg("local")), "r2": bool(self.leg("r2"))},
        }
        if with_source:
            src = self.source
            d["source_path"] = str(src) if src else ""
        return d


_SETTING_TYPES = {"int": int, "list": list}


def load_config() -> Config:
    """Resolve plugin settings from the active profile's Hermes config.

    Settings live at ``plugins.entries.<plugin>.settings.<key>`` (validated
    against the manifest ``config_schema`` at load time). Secret keys are NOT
    read here — they flow through :func:`get_secret` at call time.
    """
    cfg = Config()
    try:
        from hermes_cli.config import cfg_get, load_config as load_hermes_config
        settings = cfg_get(load_hermes_config(), "plugins", "entries", PLUGIN_ID, "settings", default={}) or {}
    except Exception as exc:  # config unreadable → run on defaults, never raise
        logger.warning("hermes-backup: settings unreadable, using defaults (%s)", exc)
        settings = {}
    for key in ("source_path", "local_repo", "r2_endpoint", "r2_bucket"):
        value = settings.get(key)
        if value is not None:
            setattr(cfg, key, str(value))
    for key, typ in (("interval_minutes", int), ("local_keep_within_hours", int),
                     ("local_keep_last", int), ("r2_keep_within_hours", int),
                     ("r2_keep_last", int)):
        value = settings.get(key)
        if value is not None and not isinstance(value, bool):
            try:
                setattr(cfg, key, typ(value))
            except (TypeError, ValueError):
                pass
    if isinstance(settings.get("excludes"), list):
        cfg.excludes = [str(item) for item in settings["excludes"]]
    return cfg


# ── legs + restic runner ─────────────────────────────────────────────────────
@dataclass
class ResticResult:
    rc: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    stdout_bytes: Optional[bytes] = None  # raw capture (binary mode only)

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass
class Leg:
    leg_id: str            # "local" | "r2"
    repo: Optional[str]    # None = not configured
    secrets: Secrets

    @property
    def configured(self) -> bool:
        return bool(self.repo)

    def env(self) -> dict:
        """Fresh env per call: process env + this leg's restic identity."""
        env = dict(os.environ)
        env["RESTIC_PASSWORD"] = self.secrets.restic_password
        env["RESTIC_REPOSITORY"] = self.repo or ""
        if self.leg_id == "r2":
            env["AWS_ACCESS_KEY_ID"] = self.secrets.r2_access_key
            env["AWS_SECRET_ACCESS_KEY"] = self.secrets.r2_secret_key
        env.pop("RESTIC_PASSWORD_FILE", None)
        env.pop("RESTIC_PASSWORD_COMMAND", None)
        return env

    def run(self, argv: list[str], timeout: int, allow_no_lock: bool = False,
            binary: bool = False) -> ResticResult:
        """Run one restic argv list. ``allow_no_lock`` marks read-only calls,
        which always get ``--no-lock``: a programmatic reader must fail fast,
        never queue behind a cycle's exclusive lock. ``binary`` captures RAW
        bytes (``restic dump`` output is file content — text mode would raise
        UnicodeDecodeError on binary files and normalize CRLF; fix round 1)."""
        if shutil.which(RESTIC_BINARY) is None:
            return ResticResult(rc=-1, stderr=_restic_missing_hint())
        if allow_no_lock and "--no-lock" not in argv:
            argv = [*argv, "--no-lock"]
        try:
            proc = subprocess.run(
                argv, env=self.env(), capture_output=True, text=not binary,
                timeout=timeout)
            if binary:
                raw_out, raw_err = proc.stdout or b"", proc.stderr or b""
                return ResticResult(rc=proc.returncode,
                                    stdout=raw_out.decode("utf-8", "surrogateescape"),
                                    stderr=raw_err.decode("utf-8", "surrogateescape"),
                                    stdout_bytes=raw_out)
            return ResticResult(rc=proc.returncode, stdout=proc.stdout or "",
                                stderr=proc.stderr or "")
        except subprocess.TimeoutExpired:
            return ResticResult(rc=-1, stderr=f"restic {argv[1] if len(argv) > 1 else ''!r} timed out after {timeout}s",
                                timed_out=True)
        except OSError as exc:
            return ResticResult(rc=-1, stderr=f"failed to launch restic: {exc}")


def _restic_missing_hint() -> str:
    return ("restic is not installed. Install it first: "
            "Debian/Ubuntu `sudo apt install restic`, Fedora `sudo dnf install restic`, "
            "Arch `sudo pacman -S restic`, macOS `brew install restic` (requires restic >= 0.16).")


def legs(cfg: Config, secrets: Optional[Secrets] = None) -> list[Leg]:
    secrets = secrets or Secrets.load()
    out = []
    for leg_id in ("local", "r2"):
        out.append(Leg(leg_id=leg_id, repo=cfg.leg(leg_id), secrets=secrets))
    return out


def get_leg(cfg: Config, leg_id: str, secrets: Optional[Secrets] = None) -> Leg:
    if leg_id not in ("local", "r2"):
        raise ValueError(f"unknown leg {leg_id!r} (use 'local' or 'r2')")
    return legs(cfg, secrets)[("local", "r2").index(leg_id)]


def _repo_state(leg: Leg) -> str:
    """Fast leg health probe used before expensive work."""
    if not leg.configured:
        return "not-configured"
    if leg.leg_id == "local" and not Path(leg.repo).is_dir():
        return "unmounted"  # local repo path absent — drive likely unmounted
    if not leg.secrets.restic_password:
        return "no-password"
    return "ready"


# ── single-instance cycle lock ───────────────────────────────────────────────
def cycle_lock_path() -> Path:
    return plugin_data_dir() / "cycle.lock"


class CycleBusy(Exception):
    """A restic cycle (ours) is already active."""


def acquire_cycle_lock(timeout: float = 0.0) -> Any:
    """Non-blocking flock-based single-instance lock (port of the bash flock).

    Raises :class:`CycleBusy` when a previous cycle still holds the lock.
    Returns the open file object; caller must ``close()`` it to release.
    """
    path = cycle_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        raise CycleBusy("SKIP: previous run active")
    fd.write(f"{os.getpid()} {int(time.time())}\n")
    fd.flush()
    return fd


def restic_process_running() -> bool:
    """True when any restic process is running (``pgrep -x restic``)."""
    try:
        proc = subprocess.run(["pgrep", "-x", RESTIC_BINARY],
                              capture_output=True, text=True, timeout=10)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False  # pgrep unavailable → cannot assert; do not block on a guess


# ── cycle log ────────────────────────────────────────────────────────────────
def log_path() -> Path:
    return plugin_data_dir() / LOG_FILENAME


def log_append(line: str, stamp: bool = True) -> None:
    """Append one verdict/detail line, with a size guard (bounded rotation).

    ``stamp=False`` writes the line verbatim — used for the reference
    ``DRILL <date> <verdict> driver=<who>`` format, which is NOT a
    bracketed-cycle verdict and must not gain a second date prefix.
    """
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            with open(path, "rb") as f:
                f.seek(-LOG_KEEP_BYTES, 2)
                keep = f.read()
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(keep)
            os.replace(tmp, path)
        if stamp:
            line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{line}\n")
    except OSError as exc:
        logger.warning("hermes-backup: could not append to log: %s", exc)


def log_tail(nbytes: int = LOG_TAIL_BYTES) -> str:
    """Last ``nbytes`` of the log (verdict-to-EOF distance grows unbounded)."""
    path = log_path()
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        return f.read().decode("utf-8", "replace")


def last_run_verdict() -> Optional[dict]:
    """Most recent cycle verdict from a 64KB tail (widget-proven parse)."""
    lines = log_tail().splitlines()[-400:]
    matches = []
    for line in lines:
        m = LOG_VERDICT_RE.match(line.strip())
        if m:
            matches.append(m.groups())
    if not matches:
        return None
    stamp, status, detail = matches[-1]
    epoch = _stamp_epoch(stamp)
    return {"time": stamp, "epoch": epoch, "verdict": status, "detail": detail.strip()}


def last_drill_verdict() -> Optional[dict]:
    for line in reversed(log_tail().splitlines()[-400:]):
        m = LOG_DRILL_RE.match(line.strip())
        if m:
            date, verdict, driver = m.groups()
            epoch = _stamp_epoch(f"{date} 00:00:00")
            return {"date": date, "epoch": epoch, "verdict": verdict, "driver": driver}
    return None


def _stamp_epoch(stamp: str) -> Optional[int]:
    """Log stamps are local wall-clock; convert to numeric epoch (renderer math).

    ``time.mktime`` interprets the parsed struct as LOCAL time — the same
    zone the writer used (``time.strftime``). ``calendar.timegm`` treated it
    as UTC and shifted every timestamp on non-UTC hosts (fix round 1, P2)."""
    try:
        parsed = time.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        return int(time.mktime(parsed))
    except (ValueError, TypeError, OverflowError):
        return None


def append_drill_line(verdict: str, driver: str = "agent") -> None:
    verdict = verdict if verdict in ("PASS", "FAIL") else "FAIL"
    driver = (driver or "agent").strip().replace("\n", " ")[:64] or "agent"
    log_append(f"DRILL {time.strftime('%Y-%m-%d')} {verdict} driver={driver}", stamp=False)


# ── snapshot queries (all --no-lock) ────────────────────────────────────────
def _parse_snapshot_time(iso: str) -> Optional[int]:
    """Snapshot ``time`` is ISO with (up to 9-digit) fractional seconds —
    the renderer needs numeric epoch; truncate to microseconds first."""
    if not iso:
        return None
    try:
        from datetime import datetime
        iso = re.sub(r"\.(\d+)", lambda m: f".{m.group(1)[:6]}", iso, count=1)
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:
        return None


def _snapshots_json(leg: Leg, latest: int = 0) -> Optional[list]:
    argv = ["restic", "snapshots"]
    if latest:
        argv += ["--latest", str(latest)]
    argv += ["--json"]
    for attempt in range(2):  # one retry: lock contention can transiently fail
        result = leg.run(argv, timeout=TIMEOUTS["snapshots"], allow_no_lock=True)
        if result.ok:
            try:
                data = json.loads(result.stdout or "[]")
                return data if isinstance(data, list) else None
            except json.JSONDecodeError:
                return None
        time.sleep(2) if attempt == 0 else None
    return None


def latest_snapshot(leg: Leg) -> Optional[dict]:
    """Newest snapshot as {id, short_id, time, time_epoch, paths, tags}."""
    snaps = _snapshots_json(leg, latest=1) or []
    if not snaps:
        return None
    snap = snaps[-1]
    epoch = _parse_snapshot_time(snap.get("time", ""))
    return {
        "id": snap.get("id", ""),
        "short_id": snap.get("short_id", ""),
        "time": snap.get("time", ""),
        "time_epoch": epoch,
        "paths": snap.get("paths", []),
        "tags": snap.get("tags", []),
    }


def list_snapshots(leg: Leg) -> list[dict]:
    snaps = _snapshots_json(leg) or []
    out = []
    for snap in snaps:
        summary = snap.get("summary") or {}
        out.append({
            "id": snap.get("id", ""),
            "short_id": snap.get("short_id", ""),
            "time": snap.get("time", ""),
            "time_epoch": _parse_snapshot_time(snap.get("time", "")),
            "paths": snap.get("paths", []),
            "tags": snap.get("tags", []),
            "hostname": snap.get("hostname", ""),
            "total_bytes": summary.get("total_bytes_processed"),
        })
    out.sort(key=lambda s: s.get("time_epoch") or 0, reverse=True)
    return out


def repo_stats(leg: Leg) -> Optional[int]:
    """Repository size in bytes (raw-data mode, numeric epoch consumers)."""
    result = leg.run(["restic", "stats", "--mode", "raw-data", "--json"],
                     timeout=TIMEOUTS["stats"], allow_no_lock=True)
    if not result.ok:
        return None
    try:
        data = json.loads(result.stdout or "{}")
        return int(data.get("total_size", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def restic_check(leg: Leg, sample_pct: Optional[float] = None, deep: bool = False) -> ResticResult:
    """Structural ``restic check``; optional sampled or full data read.

    ``deep`` (full ``--read-data``) can take many minutes — never default.
    """
    argv = ["restic", "check"]
    if deep:
        argv.append("--read-data")
    elif sample_pct is not None:
        argv.append(f"--read-data-subset={sample_pct}%")
    timeout = TIMEOUTS["check_read_data"] if (deep or sample_pct) else TIMEOUTS["check"]
    return leg.run(argv, timeout=timeout, allow_no_lock=True)


def restic_dump(leg: Leg, snapshot_id: str, path: str) -> ResticResult:
    """``restic dump`` a single stored (absolute) path to stdout.

    Captured as RAW bytes (``stdout_bytes``): the output is file content, and
    text-mode capture both crashes on binary files (UnicodeDecodeError) and
    normalizes CRLF before the byte-compare (fix round 1, Greptile P1)."""
    return leg.run(["restic", "dump", snapshot_id, path], timeout=TIMEOUTS["dump"],
                   allow_no_lock=True, binary=True)


# ── hot SQLite shadow copies ─────────────────────────────────────────────────
def discover_sqlite_dbs(source: Path) -> list[Path]:
    """Hot databases: *.db/*.sqlite/*.sqlite3 within SHADOW_SCAN_DEPTH of the
    source root (the Hermes home's hot DBs: ``state.db``,
    ``profiles/<name>/state.db``), skipping cache/venv/third-party subtrees."""
    found: list[Path] = []
    if not source.is_dir():
        return found
    source = source.resolve()
    stack: list[tuple[Path, int]] = [(source, 0)]
    while stack:
        base, depth = stack.pop()
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in entries:
            candidate = base / entry
            rel_parts = candidate.relative_to(source).parts
            if SHADOW_SKIP_DIRS & set(rel_parts):
                continue
            if candidate.is_symlink():
                # P2#10 (fix round 1): lstat view — a symlinked DB (or a
                # symlinked dir) can point OUTSIDE the configured source;
                # shadow-copying it silently pulled outside data in. Skip.
                continue
            if candidate.is_dir():
                if depth < SHADOW_SCAN_DEPTH:
                    stack.append((candidate, depth + 1))
            elif candidate.is_file() and candidate.suffix.lower() in SQLITE_SUFFIXES:
                found.append(candidate)
    return sorted(set(found))


def stage_shadow_dbs(source: Optional[Path]) -> dict:
    """Shadow-copy hot SQLite DBs INTO the source tree (``<source>/.db-shadow/``).

    ``sqlite3`` backup API (online, WAL-consistent) with a plain copy fallback.
    Returns a report dict; never raises — a shadow failure must not block a
    cycle (the DBs are then just snapshotted in their on-disk state).
    """
    report: dict = {"shadow_dir": None, "copied": [], "fallback_copied": [], "failed": []}
    if source is None or not source.is_dir():
        report["error"] = "source tree missing"
        return report
    shadow = source / SHADOW_DIRNAME
    report["shadow_dir"] = str(shadow)
    try:
        shadow.mkdir(exist_ok=True)
    except OSError as exc:
        report["error"] = f"cannot create shadow dir: {exc}"
        return report
    for db_path in discover_sqlite_dbs(source):
        name = "__".join(db_path.relative_to(source).parts)
        dst = shadow / name
        try:
            with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as src, \
                    closing(sqlite3.connect(str(dst))) as dst_conn:
                src.backup(dst_conn)
            report["copied"].append(name)
        except Exception:
            try:
                shutil.copy2(db_path, dst)
                report["fallback_copied"].append(name)
            except OSError as exc:
                report["failed"].append({"file": name, "error": str(exc)})
    return report


def cleanup_shadow_dbs(source: Optional[Path]) -> bool:
    """Remove the staged shadow dir (the post-cycle cleanup trap)."""
    if source is None:
        return False
    shadow = source / SHADOW_DIRNAME
    if shadow.exists():
        shutil.rmtree(shadow, ignore_errors=True)
    return not shadow.exists()


# ── snapshot cycle ───────────────────────────────────────────────────────────
MAINTENANCE_STATE_FILENAME = "maintenance-state.json"


def _top_of_hour_window(cfg: Config, now: Optional[float] = None) -> bool:
    """True within the first ``interval_minutes`` of the hour — the throttle
    window the reference stack used for prune + the 2% data check."""
    minutes = max(1, int(cfg.interval_minutes or 5))
    now = now if now is not None else time.time()
    return time.localtime(now).tm_min < minutes


def _maintenance_state_path() -> Path:
    return plugin_data_dir() / MAINTENANCE_STATE_FILENAME


def _last_maintenance_epoch() -> Optional[float]:
    """Epoch of the last COMPLETED maintenance pass — None when the last
    attempt failed or was never made (fix round 2, P2: a failed pass must
    not consume the hourly window). The record's ``state`` distinguishes
    ``completed`` (done, repeats in this window skip) from ``failed``
    (attempted but not successful: the next auto cycle in the window
    retries). Records written before round 2 carried no ``state`` field and
    only ever marked completion."""
    try:
        data = json.loads(_maintenance_state_path().read_text())
        if not isinstance(data, dict) or data.get("state", "completed") != "completed":
            return None
        value = data.get("last_maintenance_epoch")
        return float(value) if value is not None else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _maintenance_done_this_window(now: Optional[float] = None) -> bool:
    """True when an hourly maintenance pass already COMPLETED inside the
    CURRENT top-of-hour window (fix round 1, P2: a timer firing every
    interval_minutes must not re-run forget --prune + the 2% sample 2-3
    times an hour). Recording only ever happens in-window, so matching the
    wall-clock hour is sufficient. A FAILED attempt records state="failed"
    instead, leaving the window open for the next cycle to retry (fix
    round 2)."""
    last = _last_maintenance_epoch()
    if last is None:
        return False
    now = now if now is not None else time.time()
    lt, ln = time.localtime(last), time.localtime(now)
    return (lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour) == \
           (ln.tm_year, ln.tm_mon, ln.tm_mday, ln.tm_hour)


def _record_maintenance(state: str, now: Optional[float] = None) -> None:
    """Persist the outcome of this window's maintenance attempt.

    ``completed`` closes the slot: repeats inside the window skip (round-1
    P2#9). ``failed`` leaves it open: the next auto cycle in the window
    retries the failed pass instead of reporting success without repeating
    the failed check (fix round 2). The not-due case — prune="auto" outside
    the throttle window, or no attempt at all — never writes a record.
    """
    try:
        path = _maintenance_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "state": state,
            "last_maintenance_epoch": now if now is not None else time.time()}))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("hermes-backup: could not record maintenance state: %s", exc)


def _leg_backup(leg: Leg, cfg: Config, extra_excludes: Optional[list[str]] = None) -> dict:
    """One leg's snapshot. Never raises; per-leg independence is the contract."""
    state = _repo_state(leg)
    if state != "ready":
        reason = {
            "not-configured": "leg not configured (run backup_setup)",
            "unmounted": "local repo path missing (drive unmounted?)",
            "no-password": "repository password not configured (run backup_setup)",
        }[state]
        return {"leg": leg.leg_id, "verdict": "skip", "reason": reason}
    source = cfg.source
    if source is None:  # P1#5 (fix round 1): never fall back to the CWD
        return {"leg": leg.leg_id, "verdict": "fail",
                "reason": "no backup source resolved — refusing to snapshot the "
                          "current directory (set source_path or HERMES_HOME)"}
    argv = ["restic", "backup", str(source)]
    for pattern in [*(cfg.excludes or []), *(extra_excludes or [])]:
        argv += ["--exclude", pattern]
    argv += ["--json"]
    started = time.time()
    result = leg.run(argv, timeout=TIMEOUTS["backup"])
    duration = round(time.time() - started, 1)
    if result.timed_out:
        return {"leg": leg.leg_id, "verdict": "fail",
                "reason": scrub(result.stderr or "timed out", leg.secrets),
                "duration_seconds": duration}
    snapshot_id = None
    bytes_processed = None
    if result.ok or result.rc == RESTIC_RC_VANISHED:
        snapshot_id, bytes_processed = _parse_backup_json(result.stdout)
    if result.rc == 0:
        out = {"leg": leg.leg_id, "verdict": "ok", "snapshot_id": snapshot_id,
               "duration_seconds": duration}
    elif result.rc == RESTIC_RC_VANISHED:
        # tolerated by design on a live agent home; the snapshot still lands
        out = {"leg": leg.leg_id, "verdict": "ok", "snapshot_id": snapshot_id,
               "warning": "exit code 3: source files vanished mid-read (tolerated)",
               "duration_seconds": duration}
    else:
        out = {"leg": leg.leg_id, "verdict": "fail",
               "reason": _restic_error_text(result, leg.secrets), "duration_seconds": duration}
    if bytes_processed is not None:
        out["bytes_processed"] = bytes_processed
    return out


def _parse_backup_json(stdout: str) -> tuple[Optional[str], Optional[int]]:
    """Parse ``restic backup --json`` output — an NDJSON stream of progress
    objects ending in one ``summary`` object carrying ``snapshot_id``."""
    objs: list[dict] = []
    try:
        data = json.loads(stdout or "{}")
        if isinstance(data, dict):
            objs.append(data)
    except json.JSONDecodeError:
        pass
    if not objs:
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                objs.append(obj)
    snapshot_id = None
    total = None
    for obj in objs:
        if obj.get("snapshot_id"):
            snapshot_id = str(obj["snapshot_id"]).strip() or snapshot_id
        if "total_bytes_processed" in obj:
            try:
                total = int(obj["total_bytes_processed"])
            except (TypeError, ValueError):
                pass
    return snapshot_id, total


def _restic_error_text(result: ResticResult, secrets: Optional[Secrets] = None,
                       limit: int = 400) -> str:
    """Last restic output line as a scrubbed, bounded reason string."""
    text = (result.stderr or result.stdout or "").strip()
    text = scrub(text.splitlines()[-1] if text else f"restic exit code {result.rc}", secrets)
    return text[:limit]


def _leg_forget(leg: Leg, cfg: Config) -> dict:
    if not leg.configured:
        return {"leg": leg.leg_id, "pruned": False, "reason": "not configured"}
    within_hours, keep_last = cfg.retention(leg.leg_id)
    argv = ["restic", "forget", "--prune",
            "--keep-within", f"{max(1, int(within_hours))}h",
            "--keep-last", str(max(1, int(keep_last)))]
    result = leg.run(argv, timeout=TIMEOUTS["forget"])
    return {"leg": leg.leg_id, "pruned": result.ok,
            "reason": None if result.ok else _restic_error_text(result, leg.secrets)}


def run_cycle(cfg: Optional[Config] = None, prune: str = "auto",
              secrets: Optional[Secrets] = None) -> dict:
    """One full snapshot cycle: shadow DBs → backup per leg → throttled
    prune + 2% sample → structural check of the offsite leg, every cycle.

    Single-instance (lockfile + pgrep); busy cycles SKIP cleanly instead of
    blocking. Per-leg independence: one leg's failure never gates the other.
    """
    cfg = cfg or load_config()
    secrets = secrets or Secrets.load()

    # P1#5 (fix round 1): refuse a cycle whose source cannot be resolved.
    # The old code fell back to the CWD ('restic backup .') — a scheduler job
    # would "protect" whatever directory it happened to start in and report
    # success. Loud failure instead: no backup, no success report.
    if cfg.source is None:
        reason = ("no backup source could be resolved — refusing to snapshot an "
                  "unknown directory. Set source_path in the plugin settings "
                  "(or HERMES_BACKUP_SOURCE under a standalone scheduler). "
                  "Nothing was backed up and nothing will report success.")
        log_append(f"FAIL {reason}")
        return {"verdict": "fail", "skipped": False, "reason": reason,
                "legs": {}, "detail": {}, "now_epoch": int(time.time())}

    lock = None
    try:
        lock = acquire_cycle_lock()
    except CycleBusy as busy:
        return {"verdict": "skip", "skipped": True, "reason": str(busy),
                "legs": {}, "now_epoch": int(time.time())}

    try:
        if restic_process_running():
            return {"verdict": "skip", "skipped": True,
                    "reason": "a restic cycle is already running (pgrep restic)",
                    "legs": {}, "now_epoch": int(time.time())}

        source = cfg.source
        shadow_report = stage_shadow_dbs(source)
        all_legs = legs(cfg, secrets)
        wanted = [leg for leg in all_legs if leg.configured]
        results: dict[str, dict] = {}

        try:
            for leg in wanted:  # sequential; each leg independent by try/except
                try:
                    results[leg.leg_id] = _leg_backup(leg, cfg)
                except Exception as exc:  # a leg crash must never gate the other
                    logger.exception("hermes-backup: leg %s crashed", leg.leg_id)
                    results[leg.leg_id] = {"leg": leg.leg_id, "verdict": "fail",
                                           "reason": scrub(str(exc), secrets)[:400]}
            for leg in all_legs:
                results.setdefault(leg.leg_id, {
                    "leg": leg.leg_id, "verdict": "skip", "reason": "leg not configured"})
        finally:
            cleaned = cleanup_shadow_dbs(source)
            if not cleaned:
                logger.warning("hermes-backup: shadow dir cleanup incomplete")

        verdict = _cycle_verdict(results, wanted)
        detail: dict = {"shadow": shadow_report}

        throttled = prune == "force" or (prune == "auto" and _top_of_hour_window(cfg))
        prune_results = {}
        sample_result = None
        structural = {}
        if wanted and not results.get("skipped"):
            if throttled and prune != "force" and _maintenance_done_this_window():
                # P2#9 (fix round 1): the hourly pass already ran in this
                # window — repeats are skipped, loudly.
                detail["prune_note"] = ("hourly maintenance already completed in "
                                        "this top-of-hour window — prune + 2% "
                                        "sample skipped")
            elif throttled:
                for leg in wanted:
                    try:
                        prune_results[leg.leg_id] = _leg_forget(leg, cfg)
                    except Exception as exc:
                        prune_results[leg.leg_id] = {"leg": leg.leg_id, "pruned": False,
                                                     "reason": scrub(str(exc), secrets)[:200]}
                local_leg = next((leg for leg in wanted if leg.leg_id == "local"), None)
                if local_leg and _repo_state(local_leg) == "ready":
                    sample_result = _summarize_check(restic_check(local_leg, sample_pct=2),
                                                     local_leg.secrets)
                # Fix round 2 P2: completion is recorded ONLY for work that
                # actually succeeded — every wanted leg's prune returned ok
                # AND the 2% data check passed when a local leg is
                # configured. A failed or skipped check records "failed" so
                # the next auto cycle in the window RETRIES the pass instead
                # of reporting success without repeating the failed check.
                # (force prunes unconditionally but also only closes the
                # slot on success; the not-due case never writes a record.)
                local_wanted = local_leg is not None
                sample_ok = ((not local_wanted)
                             or bool(sample_result and sample_result.get("ok")))
                pruned_ok = all(bool(r.get("pruned")) for r in prune_results.values())
                _record_maintenance(
                    "completed" if (pruned_ok and sample_ok) else "failed")
            r2_leg = next((leg for leg in wanted if leg.leg_id == "r2"), None)
            if r2_leg and _repo_state(r2_leg) == "ready":
                structural["r2"] = _summarize_check(restic_check(r2_leg), r2_leg.secrets)
        elif throttled:
            detail["prune_note"] = "no wanted legs; prune skipped"

        detail.update({"prune": prune_results, "sample_check": sample_result,
                       "structural_check": structural})

        # prune/check problems downgrade a green cycle to WARN, never the
        # reverse (backups succeeding is the primary contract)
        if verdict == "ok":
            if any(not r.get("pruned", True) for r in prune_results.values()):
                verdict = "warn"
            if sample_result and not sample_result.get("ok", True):
                verdict = "warn"
            if any(not s.get("ok", True) for s in structural.values()):
                verdict = "warn"

        _log_cycle(results, verdict, wanted, detail)
        return {"verdict": verdict, "legs": results, "detail": detail,
                "skipped": False, "now_epoch": int(time.time())}
    finally:
        try:
            if lock is not None:
                lock.close()
        except OSError:
            pass


def _summarize_check(result: ResticResult, secrets: Optional[Secrets] = None) -> dict:
    return {"ok": result.ok, "rc": result.rc,
            "detail": scrub((result.stderr or result.stdout or "").strip().splitlines()[-1]
                            if (result.stderr or result.stdout).strip()
                            else f"restic exit {result.rc}", secrets)[:200],
            "timed_out": result.timed_out}


def _cycle_verdict(results: dict, wanted: list[Leg]) -> str:
    """OK when every wanted leg succeeded; WARN when some did; FAIL when none
    did (reference taxonomy: both legs absent is a failure state)."""
    wanted_ids = [leg.leg_id for leg in wanted]
    if not wanted_ids:
        return "fail"
    verdicts = [results.get(leg_id, {}).get("verdict") for leg_id in wanted_ids]
    if all(v == "ok" for v in verdicts):
        return "ok"
    if any(v == "ok" for v in verdicts):
        return "warn"
    return "fail"


def _log_cycle(results: dict, verdict: str, wanted: list[Leg], detail: dict) -> None:
    for leg_id, res in results.items():
        if res.get("verdict") in ("warn", "fail"):
            log_append(f"{res['verdict'].upper()} {leg_id}={res.get('reason') or res.get('warning', '')}")
    wanted_counts = " ".join(
        f"{leg.leg_id}={1 if results.get(leg.leg_id, {}).get('verdict') == 'ok' else 0}"
        for leg in wanted)
    snapshot_ids = " ".join(
        f"{leg_id}={res.get('snapshot_id')}" for leg_id, res in results.items()
        if res.get("snapshot_id"))
    line = f"{verdict.upper()} ({wanted_counts or 'no-wanted-legs'})"
    if snapshot_ids:
        line += f" snapshot={snapshot_ids}"
    log_append(line)


# ── restore + commit ─────────────────────────────────────────────────────────
SCRATCH_ROOT_NAME = "scratch"


def scratch_root() -> Path:
    """The ONE directory custom restore targets may live under:
    ``<plugin data dir>/scratch/`` (fix round 1, Greptile P1#1)."""
    return plugin_data_dir() / SCRATCH_ROOT_NAME


def _scratch_dir(base: Optional[Path] = None, prefix: str = "restore") -> Path:
    root = base or scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=root))


def _resolve_custom_target(target_dir: str) -> Path:
    """Validate a user-supplied custom restore target (fix round 1, P1#1).

    ``restic restore --target <dir>`` recreates the snapshot's absolute path
    structure UNDER that dir — an unconfined target (``/``) writes at the
    original live paths, bypassing confirm and the pre-commit safety copy.
    Custom targets therefore exist ONLY inside :func:`scratch_root`: resolved
    (symlink escapes rejected), empty or new.
    """
    raw = Path(os.path.expanduser(target_dir))
    if raw.is_symlink():
        raise ValueError("target_dir is a symlink — refusing (custom targets must "
                         "live inside the plugin scratch root)")
    resolved = raw.resolve()
    root = scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    if not is_within(resolved, root):
        raise ValueError(f"target_dir must be inside the plugin scratch root ({root}) "
                         f"— refusing {target_dir!r}")
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError(f"target_dir {target_dir} exists and is not a directory")
        if any(resolved.iterdir()):
            raise ValueError(f"target_dir must be empty or new — refusing to restore "
                             f"into a dirty scratch dir ({target_dir})")
    else:
        resolved.mkdir(parents=True)
    return resolved


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _resolve_latest_snapshot(leg: Leg) -> dict:
    snap = latest_snapshot(leg)
    if not snap:
        raise ValueError(f"no snapshots exist yet on the {leg.leg_id} leg "
                         f"(run backup_now first)")
    return snap


def validate_restore_path(path: str, cfg: Config) -> Path:
    """Only absolute paths inside the source tree are restorable."""
    if not path or not path.startswith("/"):
        raise ValueError("path must be an absolute path inside the backup source tree")
    source = cfg.source
    if source is None:
        raise ValueError("backup source is not configured (run backup_setup)")
    candidate = Path(path)
    if not is_within(candidate, source):
        raise ValueError(f"path {path} is outside the backup source tree "
                         f"({source}) — refusing to restore")
    return candidate


def restore_path(cfg: Optional[Config] = None, path: str = "", leg_id: str = "local",
                 snapshot_id: str = "", target_dir: str = "") -> dict:
    """Restore a path (never over the live tree) into scratch.

    Hard guarantees: restic's own integrity (``--verify`` — restored bytes
    equal the snapshot content), path confinement, scratch-first. The
    ``matches_live`` comparison is INFORMATIONAL, never a gate: a deleted
    file has no live counterpart (``null``), and a damaged file is SUPPOSED
    to differ — the restore is the repair.
    """
    cfg = cfg or load_config()
    source = validate_restore_path(path, cfg)
    leg = get_leg(cfg, leg_id)
    if _repo_state(leg) != "ready":
        return {"ok": False, "error": f"{leg_id} leg is not ready "
                f"({_repo_state(leg)}) — run backup_setup"}
    try:
        snap = _resolve_latest_snapshot(leg) if not snapshot_id else {
            "short_id": snapshot_id, "id": snapshot_id, "time": "", "time_epoch": None}
        # P1#1 (fix round 1): custom targets are confined to the plugin
        # scratch root; the default remains a fresh scratch dir.
        scratch = _resolve_custom_target(target_dir) if target_dir else _scratch_dir()
        scratch.mkdir(parents=True, exist_ok=True)
        result = leg.run(["restic", "restore", snap["short_id"], "--include", str(source),
                          "--target", str(scratch), "--verify"],
                         timeout=TIMEOUTS["restore"], allow_no_lock=True)
        if not result.ok:
            return {"ok": False, "error": _restic_error_text(result, leg.secrets),
                    "scratch_dir": str(scratch)}
        # restic recreates the absolute path structure under --target
        restored_target = scratch.joinpath(*source.parts[1:])
        if restored_target.is_dir():
            candidates = sorted(restored_target.rglob("*"))
        else:
            candidates = [restored_target]  # a single-file restore
        files = []
        for restored in candidates:
            if not restored.is_file():
                continue
            live = Path("/") / restored.relative_to(scratch)
            entry = {"restored_path": str(restored), "live_path": str(live),
                     "size_bytes": restored.stat().st_size,
                     "sha256": _sha256(restored),
                     "live_sha256": _sha256(live) if live.is_file() else None,
                     "matches_live": None, "reason": None}
            if live.is_file():
                entry["matches_live"] = entry["sha256"] == entry["live_sha256"]
                if not entry["matches_live"]:
                    entry["reason"] = ("expected: live file differs from snapshot "
                                       "— the restore is the repair")
            else:
                entry["reason"] = "live counterpart missing (deleted since snapshot?)"
            files.append(entry)
        return {"ok": True, "leg": leg_id, "snapshot_id": snap["short_id"],
                "snapshot_time_epoch": snap.get("time_epoch"),
                "restic_verified": True,
                "scratch_dir": str(scratch), "files": files,
                "summary": {
                    "files_restored": len(files),
                    "matching_live": sum(1 for f in files if f["matches_live"] is True),
                    "differing_live": sum(1 for f in files if f["matches_live"] is False),
                    "missing_live": sum(1 for f in files if f["matches_live"] is None),
                }}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("hermes-backup: restore failed")
        return {"ok": False, "error": scrub(str(exc))[:400]}


def _sha256(path: Path) -> Optional[str]:
    import hashlib
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def restore_commit(cfg: Optional[Config] = None, target_dir: str = "",
                   confirm: bool = False) -> dict:
    """Copy verified scratch-restore files into place.

    Discipline encoded: refuses without ``confirm=true``; snapshots any live
    file about to be replaced into the plugin data dir first (one-shot safety
    copy); refuses paths outside the source tree; verifies every copied file.
    """
    cfg = cfg or load_config()
    if confirm is not True:  # literal-true only — defense in depth for the handler gate
        return {"ok": False,
                "error": "refusing to overwrite live files without confirm=true — "
                         "review the restore output first"}
    if not target_dir:
        return {"ok": False, "error": "target_dir (the scratch restore) is required"}
    scratch = Path(target_dir)
    data_root = plugin_data_dir()
    if not is_within(scratch, data_root):
        return {"ok": False,
                "error": f"target_dir must be inside the plugin data dir ({data_root}) — "
                         "scratch-first is the safety contract"}
    if not scratch.is_dir():
        return {"ok": False, "error": f"target_dir {target_dir} does not exist"}
    source = cfg.source
    if source is None:
        return {"ok": False, "error": "backup source is not configured (run backup_setup)"}

    safety_dir = data_root / f"pre-commit-{time.strftime('%Y%m%d-%H%M%S')}"
    committed, skipped, safety = [], [], []
    for restored in sorted(scratch.rglob("*")):
        if not restored.is_file():
            continue
        live = Path("/") / restored.relative_to(scratch)
        if not is_within(live, source):
            skipped.append({"path": str(live), "reason": "outside source tree"})
            continue
        # P1#6 (fix round 1): never copy a snapshot over a LIVE open SQLite
        # database — with -wal/-shm sidecars the overwrite can be torn or
        # inconsistent. Refuse the DB with quiesce guidance; the non-DB
        # commit path stays as designed.
        if live.suffix.lower() in SQLITE_SUFFIXES:
            wal = live.with_name(live.name + "-wal")
            shm = live.with_name(live.name + "-shm")
            if wal.exists() or shm.exists():
                skipped.append({
                    "path": str(live),
                    "reason": "live SQLite database has WAL sidecars (-wal/-shm) "
                              "present — quiesce the agent / close the database "
                              "first, then retry the commit"})
                continue
        restored_sha = _sha256(restored)
        if restored.is_file() and live.exists():
            live_rel = live.relative_to("/")
            safety_copy = safety_dir / live_rel
            safety_copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, safety_copy)
            safety.append({"path": str(live), "safety_copy": str(safety_copy),
                           "sha256": _sha256(safety_copy)})
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(restored, live)
        copied_sha = _sha256(live)
        committed.append({"path": str(live), "sha256": copied_sha,
                          "verified": copied_sha == restored_sha})
    ok = bool(committed) and all(c["verified"] for c in committed)
    return {"ok": ok, "committed": committed, "skipped": skipped,
            "safety_snapshot": {"dir": str(safety_dir), "files": safety},
            "error": None if ok else "no files committed or verification failed"}


# ── DR drill (the creativity edge, ported verbatim) ─────────────────────────
DRILL_MAX_PICK_ATTEMPTS = 5


def run_drill(cfg: Optional[Config] = None, deep: bool = False,
              file: str = "", driver: str = "agent", secrets: Optional[Secrets] = None) -> dict:
    """Prove restorability: snapshot ages both legs → structural checks →
    ``restic dump`` one small real file from EACH leg into scratch, byte-compare
    → PASS/FAIL per step → ``DRILL <date> <verdict> driver=<who>`` log line.

    Verdict taxonomy: per-step PASS / FAIL / SKIP, plus ``file_pick_mismatch``
    — a distinct NON-failure outcome for "the dumps came back fine but no
    candidate could be verified against live". One mismatched file is never
    evidence of a broken backup. Steps strictly sequential; ``pgrep restic``
    before each — never launch a step into an already-locked repo. ``deep``
    adds a full ``--read-data`` check of the local leg (loud warning: can
    take many minutes).
    """
    cfg = cfg or load_config()
    secrets = secrets or Secrets.load()
    steps: list[dict] = []

    def step(name: str, fn) -> dict:
        if restic_process_running():
            result = {"step": name, "verdict": "FAIL",
                      "detail": "a restic cycle is running (pgrep restic) — refusing to launch into a locked repo"}
        else:
            try:
                result = {"step": name, **fn()}
            except Exception as exc:
                logger.exception("hermes-backup: drill step %s crashed", name)
                result = {"step": name, "verdict": "FAIL", "detail": scrub(str(exc), secrets)[:400]}
        steps.append(result)
        return result

    all_legs = legs(cfg, secrets)
    ready = [leg for leg in all_legs if _repo_state(leg) == "ready"]

    # (a) last snapshot age, both legs (per-leg SKIP for legs that cannot run)
    def _ages() -> dict:
        ages: dict[str, Any] = {}
        skip_count = fail_count = 0
        latest: dict[str, Optional[dict]] = {}
        for leg in all_legs:
            if not leg.configured:
                ages[leg.leg_id] = {"verdict": "SKIP", "reason": "leg not configured"}
                skip_count += 1
            elif _repo_state(leg) != "ready":
                ages[leg.leg_id] = {"verdict": "SKIP", "reason": f"not ready ({_repo_state(leg)})"}
                skip_count += 1
            else:
                snap = latest_snapshot(leg)
                latest[leg.leg_id] = snap
                if not snap:
                    ages[leg.leg_id] = {"verdict": "FAIL", "reason": "no snapshots yet"}
                    fail_count += 1
                else:
                    ages[leg.leg_id] = {"verdict": "PASS", "snapshot_id": snap["short_id"],
                                        "age_seconds": int(time.time()) - (snap.get("time_epoch") or 0)}
        if fail_count:
            verdict = "FAIL"
        elif skip_count == len(all_legs):
            verdict = "SKIP"  # nothing drilled at all
        else:
            verdict = "PASS"
        return {"verdict": verdict, "ages": ages, "latest": latest}

    ages_step = step("snapshot_age_both_legs", _ages)
    latest_by_leg: dict[str, Optional[dict]] = ages_step.get("latest", {})
    cutoff = min((s["time_epoch"] for s in latest_by_leg.values()
                  if s and s.get("time_epoch")), default=None)

    # (b) structural checks (deep adds full --read-data on the local leg)
    def _checks() -> dict:
        out: dict[str, Any] = {}
        fail = skip = ran = 0
        for leg in all_legs:
            if not leg.configured or _repo_state(leg) != "ready":
                out[leg.leg_id] = {"verdict": "SKIP", "reason": "leg not ready"}
                skip += 1
                continue
            ran += 1
            is_deep = deep and leg.leg_id == "local"
            check = restic_check(leg, deep=is_deep)
            out[leg.leg_id] = {"verdict": "PASS" if check.ok else "FAIL",
                               "deep": is_deep,
                               "detail": _summarize_check(check, leg.secrets)["detail"]}
            if not check.ok:
                fail += 1
        if fail:
            verdict = "FAIL"
        elif not ran:
            verdict = "SKIP"
        else:
            verdict = "PASS"
        return {"verdict": verdict, "checks": out}

    step("structural_checks_both_legs", _checks)
    deep_warning = ("deep=true runs a FULL read of every byte on the local leg "
                    "— can take many minutes on large repositories") if deep else None

    # (c) dump one small real file from each ready leg; byte-compare across
    # legs AND vs live — with re-pick on live mismatch (file_pick_mismatch)
    def _dump_test() -> dict:
        source = cfg.source
        candidates = _drill_candidates(source, file)
        if source is None:
            return {"verdict": "SKIP", "detail": "no source configured"}
        if not candidates:
            return {"verdict": "SKIP",
                    "detail": f"no small real file found under {source} to dump-test"}
        dumpable_legs = [leg for leg in ready
                         if (latest_by_leg.get(leg.leg_id) or {}).get("short_id")]
        if not dumpable_legs:
            return {"verdict": "SKIP", "detail": "no ready leg with snapshots to dump from"}
        attempts: list[dict] = []
        for rel in candidates[:DRILL_MAX_PICK_ATTEMPTS]:
            target = source / rel
            leg_results: dict[str, Any] = {}
            dump_bytes: dict[str, bytes] = {}
            hard_failure: dict[str, str] = {}
            for leg in dumpable_legs:
                short_id = (latest_by_leg.get(leg.leg_id) or {}).get("short_id") or ""
                if not short_id:
                    continue
                dump = restic_dump(leg, short_id, str(target))
                if not dump.ok:
                    detail = _restic_error_text(dump, leg.secrets)
                    if _dump_absence(detail):
                        # the candidate simply is not in that snapshot
                        # (excluded, or created after it) — re-pick
                        leg_results[leg.leg_id] = {"ok": False, "absent": True,
                                                   "detail": detail}
                        continue
                    leg_results[leg.leg_id] = {"ok": False, "detail": detail}
                    hard_failure[leg.leg_id] = detail
                    continue
                data = (dump.stdout_bytes if dump.stdout_bytes is not None
                        else dump.stdout.encode("utf-8", "surrogateescape"))
                dump_bytes[leg.leg_id] = data
                leg_results[leg.leg_id] = {"ok": True, "file": str(target),
                                           "size_bytes": len(data),
                                           "sha256": _sha256_bytes(data)}
            if hard_failure:
                return {"verdict": "FAIL", "file": str(target), "legs": leg_results,
                        "detail": "a ready leg could not read the file from its "
                                  "snapshot — this IS a backup problem"}
            if not dump_bytes:
                # every leg: candidate absent from the snapshot → re-pick
                attempts.append({"file": str(target), "verdict": "file_pick_mismatch",
                                 "reason": "file not present in the snapshot "
                                           "(excluded, or created after it)"})
                continue
            shas = {_sha256_bytes(data) for data in dump_bytes.values()}
            # P1#4 (fix round 1): EVERY selected leg must supply the candidate
            # before a cross-leg match is true — one matching leg must never
            # mask a leg that could not even serve the file.
            supplied_by_every_leg = len(dump_bytes) == len(dumpable_legs)
            cross_leg_match = (supplied_by_every_leg
                               and len(shas) == 1 and None not in shas)
            live_bytes = _read_bytes(target)
            live_sha = _sha256_bytes(live_bytes) if live_bytes is not None else None
            matches_live = live_sha is not None and live_sha in shas
            attempt = {"file": str(target), "legs": leg_results,
                       "legs_supplied": sorted(dump_bytes),
                       "cross_leg_match": cross_leg_match,
                       "live_exists": live_bytes is not None,
                       "matches_live": matches_live if live_bytes is not None else None,
                       "live_sha256": live_sha,
                       "mtime_eligible": _mtime_eligible(target, cutoff)}
            if cross_leg_match and matches_live:
                attempt["verdict"] = "PASS"
                return {"verdict": "PASS", "file": str(target), "attempt": attempt,
                        "attempts": attempts}
            attempts.append({**attempt, "verdict": "file_pick_mismatch"})
        # dumps came back fine from every ready leg; no candidate verified
        # against live — that is a PICK problem, not a backup problem
        return {"verdict": "file_pick_mismatch", "attempts": attempts,
                "detail": ("dumps succeeded from every ready leg but no candidate "
                           "file could be verified against live — the verification "
                           "file likely changed after the last snapshot. This is "
                           "NOT evidence of a broken backup; pick a stable file "
                           "or rerun after the next cycle.")}

    step("dump_and_byte_compare", _dump_test)

    hard_fail = any(s.get("verdict") == "FAIL" for s in steps)
    verdict = "FAIL" if hard_fail else "PASS"
    append_drill_line(verdict, driver)
    return {"verdict": verdict, "steps": steps, "deep": bool(deep),
            "deep_warning": deep_warning, "driver": driver, "now_epoch": int(time.time())}


_DUMP_ABSENCE_PATTERNS = ("does not exist", "no such file", "not found",
                          "wrong path", "is not in the snapshot")


def _dump_absence(detail: str) -> bool:
    """True when a dump error means 'path not in that snapshot' (re-pick)
    rather than a read/integrity failure (real backup problem)."""
    lowered = (detail or "").lower()
    return any(pattern in lowered for pattern in _DUMP_ABSENCE_PATTERNS)


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _mtime_eligible(path: Path, cutoff_epoch: Optional[int]) -> Optional[bool]:
    """True when the file's mtime predates the snapshot being tested (i.e.
    unchanged since then). None when there is no cutoff to compare against."""
    if cutoff_epoch is None:
        return None
    try:
        return int(path.stat().st_mtime) <= cutoff_epoch
    except OSError:
        return False


def _drill_candidates(source: Optional[Path], override: str = "") -> list[str]:
    """Small real files (1B–256KB, depth ≤ 3) ordered for the dump test —
    ``config.yaml`` preferred, mtime-eligible candidates first."""
    if source is None or not source.is_dir():
        return []
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = source / override
        resolved = candidate.resolve()
        if not (is_within(resolved, source) and resolved.is_file()
                and 0 < resolved.stat().st_size <= 256 * 1024):
            return []
        return [str(resolved.relative_to(source))]
    eligible, rest = [], []
    for path in sorted(source.rglob("*")):
        if not (path.is_file() and path.suffix.lower() not in SQLITE_SUFFIXES
                and SHADOW_DIRNAME not in path.parts and not path.is_symlink()
                and 0 < path.stat().st_size <= 256 * 1024):
            continue
        rel = path.relative_to(source)
        if len(rel.parts) > 3:
            continue
        if path.name == "config.yaml" and path.parent == source:
            eligible.insert(0, str(rel))
        else:
            rest.append(str(rel))
    return eligible + rest


def _sha256_bytes(data: bytes) -> Optional[str]:
    import hashlib
    return hashlib.sha256(data).hexdigest()


# ── timer state (numeric epochs; renderer does the math) ─────────────────────
TIMER_UNIT = "hermes-backup.timer"
SERVICE_UNIT = "hermes-backup.service"
LAUNCHD_LABEL = "com.hermes-backup.cycle"
# launchd cannot inline secrets: the plist references THIS file (restic's
# native RESTIC_PASSWORD_FILE), the human creates it (chmod 600), and
# Secrets.load reads it only in standalone scheduler mode. P1#7, fix round 1.
RESTIC_PASSWORD_FILE_PATH = "~/.config/hermes-backup/restic-password"


def timer_state() -> dict:
    """OS scheduler state for the installed timer (Linux systemd user unit,
    macOS launchd). Best-effort; numeric ``next_epoch`` when schedulable."""
    state: dict = {"kind": None, "active": False, "next_epoch": None}
    if shutil.which("systemctl"):
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "show", TIMER_UNIT,
                 "--property=ActiveState", "--property=NextElapseUSecRealtime"],
                capture_output=True, text=True, timeout=10)
            out = proc.stdout or ""
            active = "ActiveState=active" in out
            nxt = re.search(r"NextElapseUSecRealtime=(\d+)", out)
            state.update(kind="systemd", active=active,
                         next_epoch=(int(nxt.group(1)) / 1e6) if nxt and active else None)
            return state
        except (OSError, subprocess.SubprocessError):
            pass
    if shutil.which("launchctl"):
        try:
            proc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
                                  capture_output=True, text=True, timeout=10)
            state.update(kind="launchd", active=proc.returncode == 0)
        except (OSError, subprocess.SubprocessError):
            pass
    return state


# ── status (60s cache; numeric epochs) ───────────────────────────────────────
def build_status(cfg: Optional[Config] = None, force: bool = False,
                 secrets: Optional[Secrets] = None) -> dict:
    """Full status across both legs; cached 60s so pollers never spawn restic
    subprocesses per tick. All timestamps numeric epoch."""
    cache_file = plugin_data_dir() / STATUS_CACHE_FILENAME
    now = time.time()
    if not force:
        try:
            cached = json.loads(cache_file.read_text())
            if now - cached.get("ts", 0) < CACHE_TTL_SECONDS:
                data = cached.get("data", {})
                data["cache_age_seconds"] = round(now - cached.get("ts", 0), 1)
                return data
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    cfg = cfg or load_config()
    secrets = secrets or Secrets.load()
    source = cfg.source
    legs_out: dict[str, dict] = {}
    for leg in legs(cfg, secrets):
        entry: dict[str, Any] = {"enabled": leg.configured}
        if not leg.configured:
            entry["note"] = "not configured — run backup_setup to enable this leg"
            legs_out[leg.leg_id] = entry
            continue
        state = _repo_state(leg)
        entry["state"] = state
        snap = latest_snapshot(leg) if state == "ready" else None
        if snap:
            entry["last_snapshot"] = {
                "id": snap["short_id"], "time": snap["time"],
                "time_epoch": snap["time_epoch"],
                "age_seconds": int(now - snap["time_epoch"]) if snap["time_epoch"] else None,
            }
            interval = max(1, int(cfg.interval_minutes or 5))
            entry["rpo"] = {
                "interval_minutes": interval,
                "behind_schedule": bool(snap["time_epoch"]
                                        and now - snap["time_epoch"] > 2 * interval * 60),
            }
        else:
            entry["last_snapshot"] = None
            if state == "ready":
                entry["note"] = "repo reachable but has no snapshots yet — run backup_now"
        entry["repo_size_bytes"] = repo_stats(leg) if state == "ready" else None
        legs_out[leg.leg_id] = entry

    configured = any(l["enabled"] for l in legs_out.values())
    data = {
        "configured": configured,
        "source_path": str(source) if source else "",
        "legs": legs_out,
        "last_run": last_run_verdict(),
        "last_drill": last_drill_verdict(),
        "timer": timer_state(),
        "rpo_interval_minutes": max(1, int(cfg.interval_minutes or 5)),
        "now_epoch": int(now),
    }
    if not configured:
        data["guidance"] = ("Nothing is configured yet: run the backup_setup tool "
                            "to create the restic repos and print the exact "
                            "scheduler setup (systemd timer, launchd, or a "
                            "Hermes cron one-liner).")
    try:
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": now, "data": data}))
        os.replace(tmp, cache_file)  # atomic: two pollers can race this file
    except OSError:
        pass
    return data


# ── setup helpers (unit files, init) ─────────────────────────────────────────
def restic_missing() -> Optional[str]:
    """Install hint when restic is absent (per-OS command), else None."""
    if shutil.which(RESTIC_BINARY):
        return None
    return _restic_missing_hint()


def _on_calendar(interval_minutes: int) -> str:
    minutes = max(1, int(interval_minutes or 5))
    if 60 % minutes == 0:
        return f"*:0/{minutes}"
    return f"*-*-* *:0/{minutes}"  # systemd accepts this form; near-hour drift noted


def systemd_unit_text(interval_minutes: int) -> dict:
    """Exact systemd user unit + timer content for the chosen cadence."""
    calendar = _on_calendar(interval_minutes)
    plugin_dir = Path(__file__).resolve().parent
    python = shutil.which("python3") or "/usr/bin/python3"
    service = (
        "[Unit]\n"
        "Description=Hermes backup cycle (restic, dual-target)\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"EnvironmentFile=%h/.config/{PLUGIN_ID}.env\n"
        f"ExecStart={python} {plugin_dir / 'cycle.py'}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run the hermes-backup cycle on a schedule\n\n"
        "[Timer]\n"
        f"OnCalendar={calendar}\n"
        "Persistent=true\n"
        "Unit=hermes-backup.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    env_file = (
        "# hermes-backup scheduler env — maintain these keys yourself:\n"
        "#   HERMES_BACKUP_RESTIC_PASSWORD (required)\n"
        "#   HERMES_BACKUP_R2_ACCESS_KEY / HERMES_BACKUP_R2_SECRET_KEY (offsite leg)\n"
        "#   HERMES_BACKUP_SOURCE / HERMES_BACKUP_LOCAL_REPO / HERMES_BACKUP_R2_ENDPOINT /\n"
        "#   HERMES_BACKUP_R2_BUCKET / HERMES_BACKUP_INTERVAL_MINUTES (optional overrides)\n"
        f"#   HERMES_HOME (only for standalone scheduler runs: the Hermes home that\n"
        f"#     holds plugin-data/{PLUGIN_ID}; leave unset when run inside Hermes)\n"
        "# The cycle script reads ONLY its process env (injected here); it never\n"
        "# reads this or any other credential file directly.\n"
    )
    return {"service": service, "timer": timer, "env_file": env_file,
            "env_file_path": f"~/.config/{PLUGIN_ID}.env",
            "on_calendar": calendar}


def _hermes_home_str() -> str:
    """The Hermes home a standalone scheduler job must use (plugin-data layout)."""
    try:
        from hermes_constants import get_hermes_home

        return str(Path(get_hermes_home()).resolve())
    except Exception:
        return str(Path(os.environ.get("HERMES_HOME")
                        or Path.home() / ".hermes").resolve())


def launchd_plist_text(interval_minutes: int, cfg: Optional[Config] = None) -> dict:
    """launchd job for macOS (fix round 1, Greptile P1#7).

    launchd has no EnvironmentFile: the plist now carries the job's settings
    in ``EnvironmentVariables`` (HERMES_HOME, source, repos, cadence) plus a
    ``RESTIC_PASSWORD_FILE`` reference — a PATH, never an inlined secret
    value. ``Secrets.load`` honors that reference in standalone mode. Config
    gaps degrade LOUDLY via ``warnings``; ``setup_steps`` says exactly what a
    human must do (create the chmod-600 password file, load the agent).
    """
    cfg = cfg or load_config()
    plugin_dir = Path(__file__).resolve().parent
    python = shutil.which("python3") or "/usr/bin/python3"
    password_file = Path(RESTIC_PASSWORD_FILE_PATH).expanduser()

    env_items: dict[str, str] = {
        "HERMES_HOME": _hermes_home_str(),
        "HERMES_BACKUP_INTERVAL_MINUTES": str(max(1, int(interval_minutes or 5))),
        "RESTIC_PASSWORD_FILE": str(password_file),
    }
    source = cfg.source
    if source is not None:
        env_items["HERMES_BACKUP_SOURCE"] = str(source)
    local_repo = cfg.leg("local")
    if local_repo:
        env_items["HERMES_BACKUP_LOCAL_REPO"] = str(local_repo)
    r2_repo = cfg.leg("r2")
    if r2_repo:
        env_items["HERMES_BACKUP_R2_ENDPOINT"] = (cfg.r2_endpoint or "").strip().rstrip("/")
        env_items["HERMES_BACKUP_R2_BUCKET"] = (cfg.r2_bucket or "").strip()

    warnings: list[str] = []
    if source is None:
        warnings.append("no backup source resolved — the job will REFUSE every cycle "
                        "(fix round 1 refuses CWD backups); set source_path in the "
                        "plugin settings")
    if not (local_repo or r2_repo):
        warnings.append("no backup repository is configured — the job would fail "
                        "with setup guidance every cycle; run backup_setup first")
    if r2_repo:
        warnings.append("the offsite S3 keys CANNOT ride the launchd plist (secrets "
                        "are never inlined): under launchd the r2 leg will fail "
                        "authentication — keep the local leg as primary or cover the "
                        "offsite leg with the Hermes cron one-liner instead")

    env_xml = "".join(
        f"    <key>{xml_escape(k)}</key><string>{xml_escape(v)}</string>\n"
        for k, v in sorted(env_items.items()))
    plist = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\">\n<dict>\n"
        f"  <key>Label</key><string>{LAUNCHD_LABEL}</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        f"    <string>{xml_escape(python)}</string>\n"
        f"    <string>{xml_escape(str(plugin_dir / 'cycle.py'))}</string>\n"
        "  </array>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        f"{env_xml}"
        "  </dict>\n"
        f"  <key>StartInterval</key><integer>{max(60, int(interval_minutes) * 60)}</integer>\n"
        "  <key>RunAtLoad</key><false/>\n"
        "  <key>StandardOutPath</key><string>/tmp/hermes-backup-cycle.out</string>\n"
        "  <key>StandardErrorPath</key><string>/tmp/hermes-backup-cycle.err</string>\n"
        "</dict>\n</plist>\n"
    )
    setup_steps = [
        f"create the password file (the plugin never writes it): mkdir -p "
        f"{password_file.parent} && touch {password_file} && chmod 600 "
        f"{password_file}, then paste the repository password into it",
        f"save the plist as ~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist",
        f"load it: launchctl load ~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist",
    ]
    return {"plist": plist, "label": LAUNCHD_LABEL,
            "path": f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist",
            "environment_variables": env_items,
            "password_file": str(password_file),
            "setup_steps": setup_steps, "warnings": warnings}


def cron_one_liner(interval_minutes: int) -> str:
    minutes = max(1, int(interval_minutes or 5))
    return (f"hermes cron create '{minutes}m' --name hermes-backup --deliver origin "
            f"'Use the backup_now tool with prune set to auto and report the per-leg verdict.'")


def init_repo(cfg: Optional[Config] = None, leg_id: str = "local",
              secrets: Optional[Secrets] = None) -> dict:
    """``restic init`` on demand (idempotent-ish: an already-initialized repo
    is reported as a warning, not an error)."""
    cfg = cfg or load_config()
    secrets = secrets or Secrets.load()
    leg = get_leg(cfg, leg_id, secrets)
    state = _repo_state(leg)
    if state == "not-configured":
        return {"ok": False, "error": f"{leg_id} leg is not configured "
                "(set its repository in the plugin settings first)"}
    if state == "no-password":
        return {"ok": False, "error": "repository password not configured "
                "(set the restic_password secret, then retry)"}
    if leg.leg_id == "local":
        Path(leg.repo).mkdir(parents=True, exist_ok=True)
    result = leg.run(["restic", "init"], timeout=TIMEOUTS["init"])
    text = scrub((result.stderr or result.stdout or "").strip(), secrets)
    if result.ok:
        return {"ok": True, "leg": leg_id, "repo": leg.repo, "detail": text[:400]}
    if "already exists" in text or "already initialized" in text.lower() \
            or "config file already exists" in text.lower():
        return {"ok": True, "leg": leg_id, "repo": leg.repo,
                "detail": "repository already initialized", "already_initialized": True}
    return {"ok": False, "leg": leg_id, "error": text[:400] or f"restic exit {result.rc}"}


def install_unit(cfg: Optional[Config] = None, interval_minutes: Optional[int] = None) -> dict:
    """Write + enable the systemd user unit and timer. ONLY called with an
    explicit ``install: true`` (rule 12: the tool asks, the user approves)."""
    if shutil.which("systemctl") is None:
        return {"ok": False,
                "error": "systemctl not found — on macOS use the launchd plist from "
                         "backup_setup output (LaunchAgents) instead"}
    interval = interval_minutes or (cfg or load_config()).interval_minutes
    units = systemd_unit_text(interval)
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / SERVICE_UNIT).write_text(units["service"])
        (unit_dir / TIMER_UNIT).write_text(units["timer"])
        for cmd in (["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", TIMER_UNIT]):
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                return {"ok": False, "error": f"{cmd[-1]} failed: {proc.stderr.strip()[:300]}"}
    except OSError as exc:
        return {"ok": False, "error": f"unit install failed: {exc}"}
    return {"ok": True, "installed": [str(unit_dir / SERVICE_UNIT), str(unit_dir / TIMER_UNIT)],
            "env_file": units["env_file_path"],
            "note": f"maintain {units['env_file_path']} with the HERMES_BACKUP_* keys listed "
                    "in the env_file template"}
