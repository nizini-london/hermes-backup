"""hermes-backup tool schemas — the LLM-facing contract for the 8 backup_* tools.

Keep names exactly in sync with ``plugin.yaml`` ``provides_tools`` (catalog
rule 6: declared capabilities must match what ``register()`` registers).
Descriptions are instructions to the model: be specific about safety rules
(scratch-first restore, confirm-gated commit, throttled prune).
"""

TOOLSET = "backup"

SOURCE_PARAM = {
    "type": "string",
    "description": (
        "Absolute path to treat as the backup source for this call, overriding "
        "the configured source. Rarely needed — the plugin setting is used "
        "when omitted."),
}

LEG_PARAM = {
    "type": "string",
    "enum": ["local", "r2"],
    "description": (
        "Which backup leg to use: 'local' (mounted-disk restic repo) or 'r2' "
        "(offsite S3-compatible bucket). Defaults to 'local'."),
}


def _schema(name: str, description: str, properties: dict, required: list | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


BACKUP_STATUS_SCHEMA = _schema(
    "backup_status",
    "Report backup health across both configured legs (local disk repo + "
    "offsite S3-compatible bucket): last snapshot per leg with its numeric "
    "epoch timestamp and age, repository size, the most recent cycle verdict "
    "parsed from the backup log, the last disaster-recovery drill verdict, "
    "and the scheduler (systemd timer / launchd / cron) state. Also reports "
    "RPO health — whether the newest snapshot is older than twice the "
    "configured interval ('snapshots behind schedule'). Cached for 60 "
    "seconds, so repeated polls are cheap and never spawn restic processes. "
    "When nothing is configured the response includes guidance naming the "
    "backup_setup tool. Call this before backup_now when diagnosing.",
    {"force": {
        "type": "boolean",
        "description": "Skip the 60-second cache and re-query restic now. Default false.",
    }},
)

BACKUP_NOW_SCHEMA = _schema(
    "backup_now",
    "Run one full snapshot cycle NOW: shadow-copy hot SQLite databases into "
    "the source tree, then run 'restic backup' on each configured leg "
    "(local + offsite) with the configured excludes, honoring per-leg "
    "independence — one leg being down never blocks or skips the other. "
    "Exit code 3 (source files vanished mid-read on a live system) is "
    "tolerated and reported as a warning, not a failure. Prune (retention "
    "forget --prune) runs only on throttled 'auto' cycles near the top of "
    "the hour; pass prune='force' to prune immediately or prune='skip' to "
    "never prune. Refuses to run (returns a clean skip, never blocks) if a "
    "restic cycle is already running. Returns per-leg verdicts "
    "ok|warn|fail|skip with snapshot ids.",
    {"prune": {
        "type": "string",
        "enum": ["auto", "force", "skip"],
        "description": (
            "'auto' (default): prune only on the first cycle after the top of "
            "the hour. 'force': prune immediately. 'skip': never prune."),
    }, "source_path": SOURCE_PARAM},
    ["prune"],
)

BACKUP_LIST_SCHEMA = _schema(
    "backup_list",
    "List snapshots on one backup leg (default 'local') via 'restic "
    "snapshots --json' with --no-lock so reads never block a running cycle. "
    "Each entry: snapshot id, numeric epoch time, backed-up paths, tags, "
    "hostname and processed-bytes. Newest first.",
    {"leg": LEG_PARAM},
)

BACKUP_VERIFY_SCHEMA = _schema(
    "backup_verify",
    "Verify the integrity of one backup leg's repository with 'restic "
    "check': structural (index + metadata, fast) by default. Pass "
    "sample_pct (1-100) to also decrypt and read a random subset of that "
    "percentage of the data packs — e.g. 2 for the routine 2% sample. "
    "Full 100% reads are slow; prefer backup_drill for the offsite copy's "
    "full-read ritual.",
    {"leg": LEG_PARAM, "sample_pct": {
        "type": "number",
        "description": (
            "Percentage of data packs to read and verify (1-100). Omit for a "
            "fast structural check. 2 matches the routine throttled sample."),
    }},
)

BACKUP_RESTORE_SCHEMA = _schema(
    "backup_restore",
    "THE recovery tool. Restore a file or subtree from a snapshot into a "
    "scratch directory — NEVER over the live tree. Hard guarantees: restic "
    "verifies every restored byte against the snapshot (--verify), paths "
    "outside the source tree are refused, and the live tree is never "
    "touched. The per-file matches_live field is INFORMATIONAL, never a "
    "gate: a deleted file has no live counterpart (null), and a damaged "
    "file is SUPPOSED to differ — the restore is the repair. 'snapshot_id' "
    "defaults to the newest snapshot on the chosen leg. Returns scratch_dir "
    "— review the files there, then use backup_restore_commit (with "
    "confirm=true) to move them into place.",
    {"path": {
        "type": "string",
        "description": "Absolute path inside the backup source tree to restore.",
    }, "leg": LEG_PARAM, "snapshot_id": {
        "type": "string",
        "description": "Snapshot to restore from (short or full id). Omit for the latest.",
    }, "target_dir": {
        "type": "string",
        "description": (
            "Optional scratch directory for the restore (inside the plugin "
            "data dir). Default: a fresh timestamped scratch dir."),
    }},
    ["path"],
)

BACKUP_RESTORE_COMMIT_SCHEMA = _schema(
    "backup_restore_commit",
    "Move a reviewed scratch restore into place. SAFETY-GATED: requires "
    "explicit confirm=true AND that the user has seen backup_restore's "
    "scratch output (that review is the gate — matches_live is never one: "
    "restoring a deleted or damaged file is legitimate and should not be "
    "blocked by it). Refuses any target_dir outside the plugin data dir; "
    "before overwriting a live file takes a one-shot safety copy of it "
    "under the plugin data dir (pre-commit snapshot) so a bad commit is "
    "itself reversible. Verifies every copied file's sha256.",
    {"target_dir": {
        "type": "string",
        "description": "The scratch directory a previous backup_restore returned.",
    }, "confirm": {
        "type": "boolean",
        "description": "Must be explicitly true to overwrite live files. Anything else is refused.",
    }, "source_path": SOURCE_PARAM},
    ["target_dir", "confirm"],
)

BACKUP_DRILL_SCHEMA = _schema(
    "backup_drill",
    "Disaster-recovery drill: PROVE the backups are restorable instead of "
    "assuming it. Sequentially: (1) report last-snapshot age on both legs, "
    "(2) structural check both legs, (3) 'restic dump' one small real file "
    "from EACH leg into scratch and byte-compare — across legs AND against "
    "the live copy. Steps report PASS / FAIL / SKIP, plus file_pick_mismatch "
    "— a distinct NON-failure outcome: the dumps came back fine but no "
    "candidate could be verified against live (the file changed after the "
    "last snapshot); that is never evidence of a broken backup, the tool "
    "re-picks another small file automatically. The drill appends a dated "
    "'DRILL <date> <verdict> driver=<who>' line to the backup log. "
    "deep=true additionally runs a FULL --read-data check of the local leg "
    "— reads every byte, can take many minutes; never make it the default. "
    "Refuses each step while a restic cycle is running.",
    {"deep": {
        "type": "boolean",
        "description": "Add a full --read-data integrity check of the local leg (slow — many minutes). Default false.",
    }, "file": {
        "type": "string",
        "description": "Optional specific file (absolute, inside the source tree) for the dump test. Default: a deterministic small file is picked.",
    }, "driver": {
        "type": "string",
        "description": "Who is driving this drill (goes into the log line, e.g. 'agent', 'shaun'). Default 'agent'.",
    }},
)

BACKUP_SETUP_SCHEMA = _schema(
    "backup_setup",
    "Set up / inspect the backup targets. Checks restic is installed (names "
    "the per-OS install command if missing), optionally initializes a restic "
    "repository ('local', 'r2' or 'both') via 'restic init', and prints the "
    "exact scheduler setup for the chosen cadence: the systemd user unit + "
    "timer content on Linux, the launchd plist on macOS, and always the "
    "'hermes cron create' one-liner as the cross-platform alternative. The "
    "unit/plist files are WRITTEN and enabled only when install=true — the "
    "agent proposes, the user approves; never install silently.",
    {"init": {
        "type": "string",
        "enum": ["local", "r2", "both"],
        "description": "Run 'restic init' to create the selected repository now (skipped when omitted).",
    }, "install": {
        "type": "boolean",
        "description": "Explicitly write and enable the systemd user timer (Linux) — requires user approval to set; omitted/false only prints the content.",
    }, "interval_minutes": {
        "type": "integer",
        "description": "Snapshot cadence in minutes for the printed/installed scheduler (default: the configured interval, usually 5).",
    }},
)

TOOL_SCHEMAS = (
    BACKUP_STATUS_SCHEMA,
    BACKUP_NOW_SCHEMA,
    BACKUP_LIST_SCHEMA,
    BACKUP_VERIFY_SCHEMA,
    BACKUP_RESTORE_SCHEMA,
    BACKUP_RESTORE_COMMIT_SCHEMA,
    BACKUP_DRILL_SCHEMA,
    BACKUP_SETUP_SCHEMA,
)

TOOL_NAMES = tuple(schema["name"] for schema in TOOL_SCHEMAS)
