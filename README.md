# hermes-backup

Encrypted, dual-target restic backup and **verified restore** for your Hermes
home — with a disaster-recovery drill that proves your backups actually come back.

> A backup you haven't restored is a hypothesis. This plugin runs the experiment.

Built by a fleet of Hermes agents — see [CONTRIBUTING.md](CONTRIBUTING.md) for
the build record and model attribution.

## What it does

`hermes-backup` keeps an encrypted, deduplicated copy of your Hermes home (or
any source path you configure) in **two independent repositories**:

- **Local leg** — a restic repository on a local or mounted drive.
- **Offsite leg** — a restic repository in any S3-compatible bucket (Cloudflare
  R2, AWS S3, Backblaze B2, Minio, …).

Each leg runs alone. If the local drive is unmounted, the offsite leg still
runs — and vice versa. One leg's failure never gates the other. Snapshots run
on a cadence you choose (default: every 5 minutes), so your recovery point is
measured in minutes, not last night.

And it does the thing most backup setups skip: it **proves** restores work.
The built-in drill performs a real restore into a scratch directory,
byte-compares it against the live file, and records a dated PASS/FAIL verdict.

### The tool surface

| Tool | What it does |
|---|---|
| `backup_setup` | Guided setup: restic check, repo creation, scheduler (systemd / launchd / cron) with opt-in unit install. |
| `backup_status` | Per-leg health: last snapshot age, repo size, last cycle verdict, scheduler state. Cached, so polling is cheap. |
| `backup_now` | Run one snapshot cycle now: shadow-copy hot databases, back up both legs, throttled prune. |
| `backup_list` | List snapshots per leg (ids, times, sizes, paths). |
| `backup_verify` | Structural integrity check (`restic check`); optional data sampling. |
| `backup_restore` | Restore a path **into scratch** — never over the live tree — with per-file sha256 comparison against live. |
| `backup_restore_commit` | After a scratch restore has been reviewed: copy into place, snapshotting each replaced file first. Requires the literal `confirm: true` argument — see [Approval reality](#approval-reality). |
| `backup_drill` | The DR drill: freshness, structural checks, real restore test from each leg, PASS/FAIL verdict, logged. |

## Quick start

1. **Load the plugin and call `backup_setup`.** It checks restic is installed
   and, if not, names the exact install command for your OS.
2. **Pick your targets** in the plugin config (either or both legs):
   - `local_repo` — path for the local repository. Leave empty to disable the
     local leg.
   - `r2_endpoint` + `r2_bucket` — any S3-compatible endpoint and bucket. Leave
     empty to disable the offsite leg.
   - `source_path` — what gets backed up. Empty = the active profile's Hermes
     home.
3. **Provide secrets** — `restic_password`, plus `r2_access_key` /
   `r2_secret_key` if you use the offsite leg. They are stored through Hermes'
   secret storage; never logged, never echoed back, and the plugin code never
   reads credential files. One by-design exception: the standalone scheduled
   cycle runs outside Hermes and takes its credentials from the machine — on
   Linux from an optional env file you maintain (the unit names it), on macOS
   from a password file you create (the plist points at it; see Disclosure).
4. **Initialize the repositories** — `backup_setup` runs `restic init` on
   demand.
5. **Schedule it** — `backup_setup` prints a systemd user unit + timer (Linux)
   or a launchd plist (macOS) for your cadence, plus a `hermes cron add`
   one-liner as the cross-platform alternative. The unit file is written only
   when you explicitly ask (`install: true`) — nothing is installed silently.
   macOS note: secrets never ride the plist. Keep the local leg primary and
   cover the offsite leg with the Hermes cron one-liner — the S3 keys cannot
   be injected under `launchd`.
6. **First backup, then first drill** — call `backup_now`, and once a snapshot
   exists, call `backup_drill`. If the drill doesn't PASS, you don't have a
   backup yet — you have a hypothesis.

### Configuration reference

| Setting | Default | Meaning |
|---|---|---|
| `source_path` | active profile's Hermes home | What gets backed up. |
| `local_repo` | *(empty = leg disabled)* | Path to the local restic repository. |
| `r2_endpoint` / `r2_bucket` | *(empty = leg disabled)* | S3-compatible endpoint + bucket for the offsite repository. |
| `restic_password` | — | Repository password (client-side encryption). One password protects the whole repo set. |
| `r2_access_key` / `r2_secret_key` | — | S3 credentials for the offsite leg. |
| `interval_minutes` | 5 | Snapshot cadence. |
| `local_keep_within_hours` / `local_keep_last` | 48 / 12 | Local retention: keep everything within the window plus at least N snapshots. |
| `r2_keep_within_hours` / `r2_keep_last` | 72 / 48 | Offsite retention. |
| `excludes` | caches, checkpoints, browser screenshots, … | Restic exclude patterns applied to every snapshot. |

## How it works

### Dual-leg independence

The local and offsite repositories are fully independent restic repos. Every
cycle attempts each leg separately and reports per-leg verdicts
(`ok` / `warn` / `fail` / `skip`). An unmounted local drive is a `skip` for
that leg — the offsite leg still runs. A dead offsite endpoint never stops the
local leg. A cycle reports success only for the legs that were wanted *and*
actually succeeded.

### Shadow copies for hot databases

An agent home contains SQLite databases that are being written while the backup
runs; copying them mid-write can produce corrupt archives. Before every
snapshot, live SQLite databases are staged **inside the source tree** under a
`.db-shadow/` directory using SQLite's own consistent backup mechanism (with a
plain copy as fallback), included in the snapshot, and cleaned up afterwards.
The archive always holds a database that opens cleanly.

### Throttled prune and integrity checks

Pruning and data-sampling checks are throttled (top-of-hour-style cycles)
instead of running on every snapshot, so a 5-minute cadence stays cheap:

- **Every cycle**: backup to both legs, structural check of the offsite repo.
- **Throttled cycles**: prune with your retention policy, plus a small
  data-sample check on the local leg.

### Restores go to scratch, never over live files

`backup_restore` restores into a fresh scratch directory, refuses paths outside
the source tree, and returns per-file sha256 checksums with a `matches_live`
verdict against the live counterparts. `backup_restore_commit` is the only step
that copies files into place: it refuses to run without the literal
`confirm: true` argument, and it snapshots every live file it is about to
replace first. The confirm gate is enforced by the tool itself — who passes
`confirm: true`, and whether a human is prompted first, is covered under
[Approval reality](#approval-reality).

### Approval reality

Read this before trusting the restore gate. `backup_restore_commit` — the one
tool that overwrites live files — has a strict, always-on agent-side gate: it
refuses to run unless the call carries the literal boolean `confirm: true`,
and it snapshots every live file it is about to replace first. That gate,
however, is enforced inside the tool. The agent that is authorized to call
the tool is also the party that passes `confirm: true`. Hermes' default
`smart` approval mode auto-approves the tools it classifies as safe, so by
default **no human prompt fires** before a restore commit — a human prompt
appears only when your Hermes home runs a stricter approval mode (per-tool or
always-ask). The plugin registration API (`ctx.register_tool`) currently
exposes no mutating/approval hint a plugin could set to force that prompt, so
this disclosure — not an upstream knob — is where the guarantee lives today.
The same pattern governs `backup_setup` with `install: true`: the systemd
unit is written only when that literal argument is present, and it is the
agent that passes it.

## The drill

Backup tools love to report "backup complete." Complete is not the same as
restorable: a snapshot list proves nothing, and even an index check can pass
while data is quietly lost. The only real evidence that a backup works is
restoring from it — and that is what `backup_drill` does, step by step:

1. **Freshness** — how old is the newest snapshot on each leg? A drill against
   stale snapshots proves nothing.
2. **Structural check** — `restic check` on both legs.
3. **The restore test** — one small, real file is restored from *each* leg into
   scratch and byte-compared against the live file.
4. **Verdict** — PASS/FAIL per step, appended to the backup log as a dated
   line: `DRILL <date> <verdict>`.

A green drill is dated proof that your offsite copy — the one you have never
touched — can actually produce a byte-identical file. Run it regularly (weekly
is a good cadence). `deep: true` adds a full data read of the local leg —
thorough, but it can take many minutes, so it is never the default.

## Disclosure

This plugin takes the following actions, so you can decide with open eyes:

- **Subprocesses**: it spawns `restic` command-line processes, running with
  your user's permissions and access.
- **Filesystem**: it reads and writes inside the configured backup source tree
  — including a temporary `.db-shadow/` staging directory during cycles,
  removed after each cycle.
- **Network**: it talks only to the S3 endpoint you configure (and only if the
  offsite leg is configured). No other network calls, no telemetry.
- **System changes**: on Linux it can install a systemd **user** unit + timer,
  but only when you explicitly request it via `backup_setup` with
  `install: true`. On macOS it prints a launchd plist for you to install.
  Nothing is installed silently.
- **Secrets**: the repository password and S3 keys are stored through Hermes'
  secret storage (plugin config secrets) — never logged, never included in tool
  output, and the plugin code never opens a credential file. The one exception
  is the standalone scheduled cycle: it runs outside Hermes, and each OS
  delivers credentials its own way. On Linux the unit declares an optional
  `EnvironmentFile=` — the env file you maintain; `backup_setup` warns when it
  is absent and a missing file fails the cycle visibly in `status` — never
  silently. On macOS, `launchd` has no env-file mechanism: the generated plist
  instead carries a `RESTIC_PASSWORD_FILE` reference pointing at a password
  file you create (`chmod 600`, `~/.config/hermes-backup/restic-password`),
  which `Secrets.load` reads directly in standalone mode.

## Requirements

- **restic ≥ 0.16** — `backup_setup` checks and names the install command
  (`apt install restic`, `brew install restic`, …).
- **Linux or macOS.** Scheduling uses a systemd user timer (Linux), launchd
  (macOS), or a Hermes cron job (both).
- **WSL2** works as Linux when systemd is enabled (default on recent Windows
  11; `/etc/wsl.conf` → `[boot] systemd=true`).
- A Hermes installation with plugin support, and an S3-compatible bucket if you
  want the offsite leg.

**Bundled skills**: the plugin ships two skills — `restore` and `drill` — that
teach raw-restic recovery and the drill protocol. Even if the agent itself is
broken, a human plus restic can still follow them and get your files back.

## FAQ

**I lost the repository password. Can my backups be recovered?**
No. Restic encrypts repositories client-side with that password, and there is
no recovery path. One password protects the whole repo set — losing it loses
the backups. Store it like the most important file on the machine, because it
is.

**Can a restore accidentally overwrite my live files?**
Not by default, and never blindly. Restores land in a scratch directory first,
with a per-file byte comparison against the live copies. Only
`backup_restore_commit` writes into the live tree: it refuses without the
literal `confirm: true` argument and snapshots every live file it is about to
replace first. That gate is agent-side — the agent passes `confirm: true`
itself, and Hermes' default `smart` approval mode does not prompt you first
(see [Approval reality](#approval-reality)).

**What happens when the local drive is unmounted?**
The local leg reports `skip` and the offsite leg keeps running — legs are
independent. If *both* legs are skipping or silent, that is a failure state to
investigate, not a quiet idle.

**What does "files vanished" (exit code 3) mean?**
Restic's warning that source files changed or disappeared mid-read — normal on
a live agent home, tolerated by design. Any other non-zero exit is a real
failure.

**Which files are excluded by default?**
Regenerable state: cache directories, image/audio/document caches, browser
screenshots, checkpoint and snapshot directories, the agent's own runtime
directory, and `.cache`. All configurable via the `excludes` setting.

**Does the backup lock my Hermes while running?**
No. Read commands run with `--no-lock` and fail fast instead of queueing behind
a cycle, and a new `backup_now` refuses to start (with a clear reason) when one
of YOUR repositories is busy — restic's own lock files in the local repository,
or a restic process whose command line or environment references your
repositories. A restic stack for a different repository on the same machine
never delays yours.

**Can I use an S3 provider other than Cloudflare R2?**
Yes — any S3-compatible endpoint works. Configure `r2_endpoint` and
`r2_bucket` with that provider's URL and keys.

**How often should I run the drill?**
Weekly is a good default. The drill's log line gives you a dated chain of
evidence: a run of green `DRILL <date> PASS` entries is the strongest single
argument that your backups are real.
