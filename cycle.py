#!/usr/bin/env python3
"""Standalone backup-cycle entry point for the scheduler (systemd/launchd).

Runs one engine cycle with ``prune="auto"`` (top-of-hour throttle) and exits
0 on OK/WARN/skip, 1 on FAIL — so ``OnFailure=``/cron failure notices work.
Runs OUTSIDE Hermes: secrets arrive as process env injected by the init
system's ``EnvironmentFile`` (never read from a credential file here) and are
fetched through the same engine helper the plugin tools use.

Usage: ``python3 cycle.py`` (optionally ``--prune force|skip``, ``--json``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine  # noqa: E402  (same directory)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one hermes-backup cycle")
    parser.add_argument("--prune", choices=("auto", "force", "skip"), default="auto")
    parser.add_argument("--json", action="store_true", help="print the full result JSON")
    args = parser.parse_args()

    cfg = engine.load_config()
    # Standalone overrides (set via the scheduler EnvironmentFile only).
    if os.environ.get("HERMES_BACKUP_SOURCE"):
        cfg.source_path = os.environ["HERMES_BACKUP_SOURCE"]
    if os.environ.get("HERMES_BACKUP_LOCAL_REPO"):
        cfg.local_repo = os.environ["HERMES_BACKUP_LOCAL_REPO"]
    if os.environ.get("HERMES_BACKUP_R2_ENDPOINT"):
        cfg.r2_endpoint = os.environ["HERMES_BACKUP_R2_ENDPOINT"]
    if os.environ.get("HERMES_BACKUP_R2_BUCKET"):
        cfg.r2_bucket = os.environ["HERMES_BACKUP_R2_BUCKET"]
    if os.environ.get("HERMES_BACKUP_INTERVAL_MINUTES"):
        try:
            cfg.interval_minutes = int(os.environ["HERMES_BACKUP_INTERVAL_MINUTES"])
        except ValueError:
            pass

    result = engine.run_cycle(cfg, prune=args.prune)
    line = json.dumps(result) if args.json else (
        f"hermes-backup: {result.get('verdict', 'unknown').upper()} "
        + " ".join(f"{leg}={res.get('verdict')}" for leg, res in (result.get("legs") or {}).items())
        + (f" ({result.get('reason')})" if result.get("reason") else ""))
    print(line, flush=True)
    return 0 if result.get("verdict") in ("ok", "warn", "skip") else 1


if __name__ == "__main__":
    raise SystemExit(main())
