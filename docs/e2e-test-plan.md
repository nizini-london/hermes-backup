# End-to-end test plan — fresh install on zeus (Nov 9 gate)

Purpose: prove the plugin works on a machine that has never seen it, before
the demo video and the catalog PR. Every step produces real output; the gate
is all-green output, pasted into the QA card.

## The one rule

EVERY `hermes` invocation below carries `HERMES_HOME="$TEST_HOME"` — install,
config, doctor, tools, dashboard, removal. Nothing in this plan touches the
real home, and no command is allowed to run without the env prefix.

## Setup (throwaway, zero contact with the real stack)

```bash
export TEST_HOME=/tmp/hb-e2e-home
export RESTIC_PASSWORD=hb-e2e-test                 # env-routed; restic -p is a FILE flag
rm -rf "$TEST_HOME" /tmp/hb-e2e-repo /tmp/hb-e2e-restore
restic init -r /tmp/hb-e2e-repo                    # scratch local repo
HERMES_HOME="$TEST_HOME" hermes plugins install /path/to/hermes-backup
HERMES_HOME="$TEST_HOME" hermes plugins doctor hermes-backup --ci
```

## Steps

1. **Configure** (all with the env prefix; non-secret keys via
   `HERMES_HOME="$TEST_HOME" hermes config set
   plugins.entries.hermes-backup.settings.<key> <value>`):
   `local_repo=/tmp/hb-e2e-repo`, `source_path=$TEST_HOME`,
   `interval_minutes=5`. The secret: put `HERMES_BACKUP_RESTIC_PASSWORD=hb-e2e-test`
   in `$TEST_HOME/.env` (Hermes's own credential route — the plugin reads it
   only through `agent.secret_scope.get_secret`, never the file). Offsite leg
   intentionally unconfigured — expect `r2=skip` verdicts everywhere.
2. **`backup_now`** → shadow-copy rules visible: SQLite DBs staged under
   `$TEST_HOME/.db-shadow/` during the run, cleaned after; verdict
   `OK (local=1 r2=skip)`; snapshot id returned.
3. **`backup_status`** → local leg fresh (age seconds), cached response (second
   call inside 60s spawns no restic subprocess — check the process table).
4. **Restore of a DELETED file** — the file lives INSIDE the source tree:
   `$TEST_HOME/notes/identity.md`. Create it, run `backup_now`, then `rm` it.
   `backup_restore path=$TEST_HOME/notes/identity.md` → lands in scratch,
   `matches_live: null` (no live counterpart — expected), content verifiable
   by opening it. Then `backup_restore_commit confirm=true` → file back in place.
5. **Negative test — path confinement**: `backup_restore` with a path OUTSIDE
   the source tree (e.g. `$HOME/notes/identity.md`) → expect an explicit
   refusal. The guard is a feature; the plan proves it.
6. **Restore of a DAMAGED file** — snapshot, then corrupt
   `$TEST_HOME/config.yaml`; restore from the pre-damage snapshot →
   `matches_live: false` with the repair reason; commit → good version back.
7. **`backup_drill`** → freshness PASS (local present, r2 skip), structural
   check PASS, dump-compare PASS with a file unchanged since the snapshot;
   verdict line `DRILL <date> PASS ...` appended to the log.
8. **Widget** — `HERMES_HOME="$TEST_HOME" hermes desktop` (web dashboard
   `hermes dashboard` if the desktop app cannot bind the throwaway home):
   per-leg health (local green, r2 skip state), RPO indicator, last drill
   verdict. Screenshot → catalog `screenshots`.
9. **Uninstall hygiene** — `HERMES_HOME="$TEST_HOME" hermes plugins remove
   hermes-backup`; confirm no stray processes, no leftover locks.

## Failure handling

Any step failing → fix on a branch, PR, Greptile, merge, re-run the whole
plan from setup (the throwaway home makes re-runs cheap).
