# Demo video — storyboard (< 90 seconds, X/Twitter submission)

Format: screen recording, terminal + Desktop widget. Recorded Nov 10 after the
e2e plan passes. Edit via the in-house shorts pipeline. Hard cap 90s.

## Beat map

| Time | Shot | Content |
|---|---|---|
| 0–8s | terminal | `ls ~/.hermes` — "Everything Hermes is — memory, skills, sessions, config — lives in one directory. Now watch what happens when you lose part of it." |
| 8–20s | terminal | Install + setup: `hermes plugins install nizini-london/hermes-backup`, settings filled, `backup_setup` output: repo created, timer/cron installed. |
| 20–35s | terminal | `backup_now` — live cycle: shadow copies of hot SQLite DBs, both legs snapshot, snapshot ids printed, verdict line. |
| 35–55s | terminal | **The moment**: `rm` a memory file → agent notices → `backup_restore` into scratch with sha256 shown → `backup_restore_commit` → file back, agent whole again. One take, no cuts inside this beat. |
| 55–75s | terminal | `backup_drill` — freshness, structural check, real dump-compare from each leg → dated PASS verdict appended to the log. Voiceover: "A backup you haven't restored is a hypothesis. This one runs the experiment — and logs the verdict." |
| 75–90s | Desktop | Widget pane: both legs green, RPO timer, last drill date. End card: `hermes plugins install hermes-backup`. |

## Rules for the shoot

- Terminal font + theme consistent; zoom on the verdict lines, not the whole
  screen; every command pre-typed (no dead air), each beat one take.
- No staged data: the file deleted and restored in beat 4 is a real file the
  agent demonstrably uses (a memory/profile file).
- Record at 2× the display for the Desktop widget so text stays legible.
- Caption the tool names on screen the first time each appears.
