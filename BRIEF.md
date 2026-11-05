# hermes-backup — build brief (the contract)

Hermes Plugin Hackathon entry, deadline **5PM EST / 10PM GMT, Tue Nov 11, 2026**.
Judges score: usefulness, reliability, polish, creativity — and favor plugins
that do more than a prompt or a skill could. A backup engine with a DR-drill
tool is exactly that. Build to catalog-admission quality, not demo quality.

Working style: every claim of "done" needs real execution output behind it.
Run the commands, paste results into the card. No dry summaries.

## What this plugin is

Encrypted restic backup + **verified restore** for the Hermes home (`~/.hermes`):
dual-target (local mounted drive + any S3-compatible bucket, e.g. Cloudflare R2),
5-minute-class RPO when scheduled, hot SQLite databases snapshotted via shadow
copies, snapshot health status, and the creativity edge — a `backup_drill` tool
that **proves** restorability (real restore into scratch, byte-level compare,
PASS/FAIL). Tagline for the README: *"A backup you haven't restored is a hypothesis."*

The engine ports a production stack that has run for weeks (skill: venus-stack-backup,
reference: `~/.hermes/profiles/venus/skills/venus-stack-backup/references/ops-and-drill.md`
— READ IT before writing engine code; its rules are distilled from real incidents).

## Catalog rules that shape the code (from plugin-catalog/README.md)

- **Rule 6** — `provides_tools` must match exactly what `register()` registers.
- **Rule 7** — `hermes plugins validate` security scan: `dangerous` fails, `caution`
  is disclosed in the PR. We will have `python_subprocess` cautions — disclose them.
- **Rule 8** — `desktop/plugin.js` may ONLY use the `@hermes/plugin-sdk` surface.
  No prototype patching, no eval, no `import()` beyond sdk/react, no script tags,
  no querying `data-slot`/`data-tour`/`data-sidebar`/`data-testid` markup, no
  `document.body` MutationObserver, no raw `window.hermesDesktop` bridge calls.
- **Rule 9** — no runtime overrides of Hermes core; only `ctx.register_*` public surfaces.
- **Rule 11** — credentials ONLY via Hermes routes: `config_schema` `type: secret`
  fields read with `agent.secret_scope.get_secret(NAME)`. NEVER read `.env`,
  `auth.json`, or any credential file. Never log/echo/return secret values.
- **Rule 12** — never route around approvals; nothing auto-approves; unattended runs
  fail cleanly rather than hang.
- **Rule 13** — risky behavior disclosed in README + PR: spawns restic subprocesses,
  reads/writes the backup source tree, network calls to the S3 endpoint only,
  installs a systemd user unit (Linux, opt-in via backup_setup).
- **Rule 14** — `requires_hermes` is an honest SemVer floor, same value in plugin.yaml.
- **Rule 17** — no binaries, no `.pyc` committed, everything reviewable source.

## Repo layout (this repo IS the plugin)

```
hermes-backup/
├── plugin.yaml          # DONE (manifest v2) — keep provides_tools in sync with code
├── BRIEF.md             # this file
├── README.md            # catalog page renders this at the pinned sha — write it for USERS
├── LICENSE              # MIT
├── __init__.py          # register(ctx): tools + bundled skills
├── schemas.py           # tool schemas (LLM-facing descriptions — be specific)
├── engine.py            # restic wrapper: every restic call via argv list, NEVER shell=True
├── tools.py             # handlers: def h(args: dict, **kwargs) -> str, always JSON, never raise
├── skills/
│   ├── restore/SKILL.md        # restore recipes (scratch-first, hash-verify, never over live tree)
│   └── drill/SKILL.md          # DR drill protocol + diagnosis order
├── desktop/plugin.js    # Desktop status pane (SDK surface only)
├── dashboard/plugin_api.py + dashboard/manifest.json   # web dashboard tab
└── tests/               # engine tests incl. a real tiny restic repo in /tmp
```

## Tool surface (8 tools — all prefixed `backup_`)

Handlers return JSON strings, always, even on error; accept `**kwargs`; never raise.
Every restic read command runs with `--no-lock` (programmatic readers fail fast,
they must not block on the cycle's exclusive lock — see skill rule). Writes
(`backup_now`) must check `pgrep restic` / lock state first and fail cleanly
with "a cycle is running" instead of blocking.

1. **`backup_status`** — per configured leg: last snapshot age (epoch ints, not
   strings — the renderer does the math), repo size, last run verdict from the
   log (regex over a LARGE 64KB tail — verdict-to-EOF distance grows without
   bound; a fixed small tail intermittently misses every verdict), timer/cron
   state, "not configured" guidance naming `backup_setup` when unconfigured.
   Must serve a cached result (60s) — multiple pollers must not spawn restic
   subprocesses per tick.
2. **`backup_now`** — run one snapshot cycle now: shadow-copy hot SQLite DBs
   INTO the backup source tree (`<source>/.db-shadow/`, sqlite3 `.backup` with
   cp fallback; cleanup trap after), restic backup with configured excludes
   (rc 3 = source-files-vanished = tolerated, anything else nonzero = fail),
   prune only if this is a top-of-hour-style throttled call (param
   `prune: "auto"|"force"|"skip"`, default auto), per-leg independence:
   one leg's failure/down state must NEVER gate or skip the other leg.
   Returns per-leg verdicts `ok|warn|fail|skip` + snapshot ids.
3. **`backup_list`** — `restic snapshots --json` per leg (paths, ids, times,
   sizes). Absolute paths as stored in the snapshot.
4. **`backup_verify`** — structural `restic check` (index+metadata) on a chosen
   leg; `sample_pct` param runs `--read-data-subset=<pct>%`.
5. **`backup_restore`** — THE flagship tool. Params: `path` (absolute, inside
   source tree), optional `snapshot_id` (default: latest), `target_dir`
   (default: fresh scratch dir under plugin_data_dir). Restores INTO SCRATCH,
   never over the live tree. Returns: file list, byte sizes, sha256 of each
   restored file, and the live counterpart's sha256 + a `matches_live` verdict
   per file. Refuses paths outside the source tree.
6. **`backup_restore_commit`** — after the user has seen/verified a scratch
   restore: copy restored files into place (requires explicit `confirm: true`
   param; refuses without it). Pre-commit: make a one-shot safety snapshot of
   any live file being replaced (restic backup --stdin or copy to plugin_data_dir).
   This is the "never restore over live files" discipline, encoded.
7. **`backup_drill`** — the creativity edge, straight from the ops reference:
   (a) report last snapshot age both legs, (b) structural check both legs,
   (c) pick one small real file, `restic dump` it from EACH leg into scratch,
   byte-compare vs live, (d) PASS/FAIL per step, (e) append a `DRILL <date>
   <verdict> driver=<who>` line to the log. `deep: true` adds full
   `--read-data` on the local leg with a loud warning it can take many minutes
   (never make this the default). Steps strictly sequential; check no restic
   cycle is running before each (pgrep), never launch a step into an
   already-locked repo.
8. **`backup_setup`** — interactive-ish guidance as a tool: checks restic is
   installed (names the install command per OS if not), creates repos
   (`restic init`) on demand, prints the exact systemd user unit + timer
   (Linux) or launchd plist (macOS) content for the chosen cadence, and the
   `hermes cron add` one-liner as the cross-platform alternative. The tool
   WRITES the unit file only when called with `install: true` (rule 12: the
   agent asks, the user approves — never install silently).

## Engine port table (bash → Python, keep every hard-won behavior)

| Source behavior | Port exactly |
|---|---|
| flock single-instance | lock via plugin_data_dir lockfile, non-blocking; busy → "SKIP: previous run active" |
| secrets from agent-secrets.env | REPLACED: `agent.secret_scope.get_secret` per rule 11 |
| shadow DBs staged INSIDE source | same: `<source>/.db-shadow/`, `sqlite3 .backup`, cp fallback, cleanup after |
| leg independence (unmounted local never blocks R2) | same: per-leg try/skip, verdicts reported per leg |
| exit code 3 tolerated | same |
| prune throttled to top of hour | same via `prune:"auto"` |
| 2% data check only on throttled cycles | same |
| R2 structural check every cycle | same |
| `--no-lock` on all programmatic reads | same |
| RESTIC_PASSWORD never logged/echoed | same — also scrub env dumps from any error output |
| verdict regex over large (64KB) log tail | same; log under plugin_data_dir |

Restic invocation: `subprocess.run(argv_list, env=..., capture_output=True,
text=True, timeout=...)`. Build env fresh from os.environ + secrets per call;
never persist secrets to disk; never include them in returned JSON or logs.
If restic is missing → actionable error naming the package/install command.

## Widget (desktop + dashboard)

Port the existing, proven widget (`~/.hermes/plugins/venus-backup/`):
desktop/plugin.js (184 lines) + dashboard/plugin_api.py (162 lines) + manifest.
- De-brand: no "Venus"/NIZINI anywhere.
- plugin_api.py endpoints must return NUMERIC epoch timestamps (renderer does
  the math — Date.parse cannot handle 9-digit fractional seconds).
- Status data cached server-side 60s; read-only restic calls use `--no-lock`.
- Secrets never returned by the status API (existing rule — keep it).
- plugin.js: audit against rule 8 while porting; theme via the host's CSS
  variables, no hardcoded colors; no core-markup queries.
- Desktop pane content: both legs' health (ok/warn/fail/skip + snapshot age),
  RPO state (" snapshots behind schedule" when age > 2× interval), last drill
  verdict + date. Buttons: "Backup now", "Run drill" — each sends a prompt
  that invokes the corresponding tool (data-hermes-send), NOT a direct call.

## Bundled skills

`skills/restore/SKILL.md` and `skills/drill/SKILL.md` — port the ops reference
content, generalized (no personal paths), registered via `ctx.register_skill`.
These make the agent competent at recovery WITHOUT the tools, which is itself
a resilience feature (if Hermes is broken, a human + restic still works).

## README (catalog page renders it)

Sections: What it does (user language) / Quick start (backup_setup walk-through)
/ How it works (shadow copies, dual-leg, throttled prune+check) / The drill
(creativity story) / Disclosure (rule 13: restic subprocesses with the user's
permissions; S3/R2 network calls; optional systemd unit install; secrets stored
via Hermes) / Requirements (restic >=0.16, Linux or macOS) / FAQ (lost password
= lost backups; restore-to-scratch design).

## Tests (tests/)

- Engine unit tests with a REAL tiny restic repo in /tmp (restic is installed
  on this machine — use it; a scratch repo init costs seconds).
- Shadow-copy logic: create temp sqlite DB with WAL, snapshot, verify shadow
  lands inside the source tree and is cleaned up.
- Leg independence: unmountable local path (nonexistent dir) → local=skip,
  r2 still runs (assert via fake restic shim on PATH for the r2 call).
- Exit-3 tolerance; verdict-regex over a log with 500 progress lines.
- Restore safety: path outside source tree → refused; restore lands in scratch.
- Run: `python -m pytest tests/ -x -q` from the repo with system python.

## Definition of done (per slice, paste real output)

- [ ] `hermes plugins validate <path> --install-deps` → all checks green (paste)
- [ ] `hermes plugins doctor <path> --ci` → clean (paste)
- [ ] `python -m pytest tests/ -q` → pass (paste)
- [ ] grep -rn 'venus\|nizini\|NIZINI' → zero hits in plugin code
- [ ] grep -rnE 'os\.environ\[|\.env|auth\.json' engine/tools → zero direct secret reads
- [ ] No `.pyc`, no binaries committed; `.gitignore` covers `__pycache__/`
