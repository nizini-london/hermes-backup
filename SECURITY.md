# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| latest `main` | yes |
| older tags | no — update |

## Reporting a vulnerability

Open a [private security advisory](https://github.com/nizini-london/hermes-backup/security/advisories/new)
or email [Shaun Xenon](mailto:shaun@nizini.com) directly. Please do not open a public issue for
exploitable behavior.

Please include: affected tool (`backup_*`), reproduction steps, and the
Hermes Agent version. We aim to acknowledge within 72 hours.

## Security design of this plugin

- **Secrets route through Hermes only.** Repository passwords and S3 keys are
  read via `agent.secret_scope.get_secret()` against `config_schema` secret
  fields. The plugin never reads `.env` files, `auth.json`, keychains, or any
  other credential store, and never logs, echoes, or returns secret values.
- **Restore is scratch-first.** `backup_restore` never writes over the live
  tree; it restores into a scratch directory, verifies file hashes against the
  live copies, and only `backup_restore_commit` (explicit `confirm: true`)
  copies files into place — keeping a safety snapshot of anything it replaces.
  The `confirm: true` gate is enforced by the tool itself, and the agent is
  the party that passes it; Hermes' default `smart` approval mode
  auto-approves tools it classifies as safe, so a human prompt is NOT
  guaranteed — use a stricter home approval mode if you want one.
- **Path confinement.** Restore paths must live inside the configured backup
  source tree. Paths outside it are refused.
- **Principle of least privilege.** The plugin shells out to `restic` (argv
  lists, never a shell) with the user's own permissions, plus — only when
  explicitly installed via `backup_setup install: true` — a systemd user unit
  under the user's own account. Nothing runs as root.
- **Unattended-run safety.** Long or interactive operations fail cleanly under
  cron and messaging runs rather than hanging; nothing auto-approves.
