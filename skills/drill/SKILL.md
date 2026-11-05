---
name: drill
description: Use when running or interpreting disaster-recovery drills for restic backups — prove restorability with a real restore test, byte comparison, and a logged PASS/FAIL verdict.
---

# The DR drill

A backup you haven't restored is a hypothesis. A snapshot list is not proof,
and an index check can pass while data on disk is quietly lost. The only
evidence that a backup is restorable is a real restore compared byte-for-byte
against the live file. The drill performs that experiment and records the
verdict.

The `backup_drill` tool automates this protocol; the commands below are what it
runs — and the manual fallback when the agent is unavailable.

## Protocol — run strictly top to bottom

Before every step, check that no restic cycle is running (process check / repo
lock); never launch a step into an already-locked repository. All programmatic
restic reads pass `--no-lock`. Finish or fail a step before starting the next.

1. **Freshness** — report the last snapshot age on both legs. A drill against
   stale snapshots proves nothing.
2. **Structural check, local leg** — `restic check` (index + metadata).
3. **Structural check, offsite leg** — `restic check`. Routine cycles never
   full-read the offsite repo, so this structural check plus the dump test in
   the next step are the offsite copy's regular evidence of health.
4. **Real restore test** — pick one small, real, currently-existing file.
   `restic dump` it from EACH leg into a scratch directory and byte-compare
   each copy against the live file.
5. **Verdict** — PASS/FAIL per step. Append
   `DRILL <date> <verdict> driver=<who>` to the backup log. A drill that is not
   logged did not happen.
6. **Human in the loop** — when possible, run drills with a human present and
   alternate who drives the commands across sessions, so neither side becomes
   a single point of ritual failure.

Cadence: weekly is a good default. `deep: true` adds a full data read
(`--read-data`) of the local leg — thorough, but it can take many minutes, so
never make it the default. For the strongest occasional assurance, a manual
full `restic check --read-data` against the offsite repo goes beyond the
automated drill, which never full-reads it.

## Interpreting results

- **All PASS** — the backup is evidence-backed restorable as of the drill date.
  Report it with the date.
- **Structural check FAIL on one leg** — that leg is untrustworthy until it
  re-checks clean. The other leg remains independent evidence; report both, and
  never let a healthy leg's PASS cover for a failed one.
- **Dump/compare FAIL** — the leg can list snapshots but cannot produce a
  byte-identical file: a restore-capability failure. Re-run against a different
  snapshot; if it persists, plan a re-seed from the other leg.

## Diagnosis order

1. **Tail the backup log first** — one verdict line per cycle
   (`OK`/`SKIP`/`WARN`/`FAIL`); it lives under the plugin's data directory.
2. **No lines at all** — check the scheduler (`systemctl --user status
   <unit>.timer` on Linux, `launchctl list` on macOS). An unmounted local drive
   skips with a WARN; the offsite leg must still run. Both legs silent or
   skipping is a failure state.
3. **Exit code 3** — restic's "source files vanished mid-read" warning,
   tolerated by design on a live agent home. Any other nonzero code is a real
   failure.
4. **Trailing `OK (local=N r2=N)`** — which legs were WANTED that cycle
   (0 = intentionally skipped), not per-leg failure counts. The OK/WARN/FAIL
   verdict is authoritative.
