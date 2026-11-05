---
name: restore
description: Use when recovering lost, damaged, or deleted files from restic backups — scratch-first restores, hash verification, never over the live tree. Covers hermes-backup tools and raw-restic recovery when the agent itself is broken.
---

# Restoring from backups

Recovery happens under pressure. Follow the discipline exactly — every shortcut
in this file has a real incident behind it.

## The one rule

**Never restore over a live tree in place.** Restore into a scratch directory
first, verify, and only then copy into place. Before any copy replaces a live
file, take a one-shot safety snapshot of that file. The `backup_restore` /
`backup_restore_commit` tools encode this discipline; the same rules apply when
working with raw restic.

## Addressing a repository

```bash
# local leg
export RESTIC_REPOSITORY=/path/to/repo

# offsite leg (any S3-compatible provider)
export RESTIC_REPOSITORY=s3:https://<endpoint>/<bucket>
export AWS_ACCESS_KEY_ID=<access key id>
export AWS_SECRET_ACCESS_KEY=<secret key>

# every leg needs the repository password
export RESTIC_PASSWORD=<from your secret store — never into files or shell history>
```

All programmatic (non-interactive) restic reads should pass `--no-lock` so they
fail fast instead of blocking on a running backup cycle.

## Finding the right snapshot

- Resolve "latest" yourself: `restic snapshots --latest 1 --json`. `--latest` is
  NOT a valid flag on `ls`, `find`, or `dump` — always resolve a concrete
  snapshot id first.
- `restic ls <snap>` lists contents. Paths must be **absolute exactly as stored
  in the snapshot**.
- Recovering deleted files: pick a snapshot that **predates the deletion** —
  compare snapshot timestamps before restoring anything.

## Recipe: single-file recovery

1. Dump the file into scratch:
   `restic dump <snap> /absolute/path/as/stored > /tmp/restore-<date>/<filename>`
2. Verify: run `sha256sum` on the restored copy and the live copy — they must
   match byte-for-byte.
3. Only then copy into place, snapshotting the live file first.

The highest-value single-file recoveries are the agent's memory, profile, and
configuration files — verify those byte-for-byte before touching anything else.

## Recipe: subtree recovery (a directory clobbered by a bad tool call)

1. Recreate just that subtree:
   `restic restore <snap> --include /absolute/path/inside/snapshot --target /tmp/restore-<date>/`
2. Verify the restored files: hash-check them, and syntax-check scripts
   (`bash -n`, `python -m py_compile`) before they go back into place.
3. Copy into place. Never restore over the live tree directly.

## Recipe: full-tree recovery (disaster)

`restic restore <snap> --target /tmp/restore-<date>/` puts the whole tree in
scratch. Inspect before replacing anything. When the agent is healthy, prefer
the `backup_restore` tool — it refuses paths outside the source tree and returns
per-file hashes with a live-comparison verdict; raw restic is the fallback when
it is not available.

## Waiting on a cycle

A legitimate cycle takes minutes (backup + data sample + upload). Foreground
wait loops shorter than a cycle time out. Either wait with a long-enough
timeout, or poll the backup log in the background — every cycle appends exactly
one verdict line (`OK`/`SKIP`/`WARN`/`FAIL`), and the log lives under the
plugin's data directory.

## Diagnosis order

1. **Tail the backup log first** — every cycle appends a verdict line.
2. **No lines at all?** The scheduler is the suspect: `systemctl --user status
   <unit>.timer` on Linux, `launchctl list` on macOS. An unmounted local drive
   makes the local leg SKIP with a WARN; the offsite leg must still run. If
   **both** legs skip or are absent, that is a failure state, not a quiet idle.
3. **Exit code 3** is restic's "source files vanished mid-read" warning —
   tolerated by design on a live agent home. Any other nonzero code is a real
   failure.
4. **A trailing `OK (local=N r2=N)`** reports which legs were WANTED that cycle
   (0 = leg intentionally skipped, e.g. drive unmounted) — not per-leg failure
   counts. Whether the wanted legs actually succeeded is the OK/WARN/FAIL
   verdict and the WARN lines above it.

## If Hermes itself is broken

This skill exists so a human plus restic can still recover. Install restic, set
`RESTIC_REPOSITORY` and `RESTIC_PASSWORD`, and follow the recipes above — the
plugin is a wrapper around exactly these primitives.
