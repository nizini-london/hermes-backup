# End-to-end test plan — fresh install on zeus (Nov 9 gate)

Purpose: prove the plugin works on a machine that has never seen it, before
the demo video and the catalog PR. Every step produces real output; the gate
is all-green output, pasted into the QA card.

## Setup (throwaway, zero contact with the real stack)

```bash
export TEST_HOME=/tmp/hb-e2e-home
rm -rf "$TEST_HOME" /tmp/hb-e2e-repo /tmp/hb-e2e-restore
restic init -r /tmp/hb-e2e-repo -p hb-e2e-test        # scratch local repo
hermes plugins install /home/shaun/repos/hermes-backup --profile e2e-test
HERMES_HOME="$TEST_HOME" hermes plugins doctor hermes-backup --ci
```

## Steps

1. **Configure** via plugin settings (Desktop form or
   `hermes config set plugins.entries.hermes-backup.settings.<key>`):
   `local_repo=/tmp/hb-e2e-repo`, `source_path=$TEST_HOME`,
   `restic_password=hb-e2e-test` (secret), `interval_minutes=5`.
   Offsite leg intentionally unconfigured — expect `r2=skip` verdicts.
2. **`backup_now`** → both shadow-copy rules visible: SQLite DBs staged under
   `$TEST_HOME/.db-shadow/` during the run, cleaned after; verdict
   `OK (local=1 r2=skip)`; snapshot id returned.
3. **`backup_status`** → local leg fresh (age seconds), cached response (second
   call inside 60s spawns no restic subprocess — check process table).
4. **Restore of a DELETED file** — write `~/notes/identity.md` into the test
   home, run `backup_now`, then `rm` it. `backup_restore path=.../identity.md`
   → lands in scratch, `matches_live: null`, content verifiable. Then
   `backup_restore_commit confirm=true` → file back in place.
5. **Restore of a DAMAGED file** — corrupt a config file, restore from the
   pre-damage snapshot → `matches_live: false` with repair reason; commit
   restores the good version.
6. **`backup_drill`** → freshness PASS both legs (local present, r2 skip),
   structural check PASS, dump-compare PASS with a file unchanged since the
   snapshot; verdict line `DRILL <date> PASS ...` appended to the log.
7. **Desktop widget** — install the desktop surface in a throwaway profile,
   relaunch the app, confirm: per-leg health (local green, r2 skip state),
   RPO indicator, last drill verdict. Screenshot → catalog `screenshots`.
8. **Uninstall hygiene** — `hermes plugins remove hermes-backup` on the test
   profile; confirm no stray processes, no leftover locks.

## Failure handling

Any step failing → fix on a branch, PR, Greptile, merge, re-run the whole
plan from step 1 (throwaway home makes re-runs cheap).
