# Contributing & build credits

This repository is built **by a fleet of Hermes agents** under human
direction — the same agent stack this plugin exists to back up. The build is
a matter of record, so here it is.

## How it is built

- Work is dispatched as Kanban slices. Each builder works in an **isolated git
  worktree branch** and never merges its own work.
- Every slice lands as a PR to `main` and must clear three gates:
  1. **Mechanical** — `hermes plugins validate --install-deps`,
     `hermes plugins doctor --ci`, and `pytest`, with the real output pasted
     into the build card.
  2. **Greptile** — every PR is reviewed by the `greptile-apps[bot]` GitHub
     App; the merge bar is **zero open findings**, with false positives
     disputed in-thread with evidence.
  3. **Cold review** — before release, a fresh agent profile that did not
     build the code reviews the integrated tree: BLOCKING / SHOULD-FIX /
     NICE-TO-HAVE / NOTE taxonomy, findings written to a file, every claim
     verified against the code before it counts.

## Build credits

| Role | Agent profile | Model | Reasoning |
| --- | --- | --- | --- |
| Engine: restic wrapper, 8 tools, tests | `stack-overflow` | `z-ai/glm-5.3-flash` (OpenRouter) | max |
| Desktop + dashboard widget | `wireframe` | `z-ai/glm-5.3-flash` (OpenRouter) | max |
| README + bundled skills | `penelope` | `z-ai/glm-5.3-flash` (OpenRouter) | max |
| Cold-review gate | `silvia` | `z-ai/glm-5.3-flash` (OpenRouter) | max |
| Orchestration, integration, gates | `venus` | `z-ai/glm-5.3-flash` (OpenRouter) | max |

- All profiles route inference with `data_collection: deny` — no provider
  that trains on or stores inputs participates in any turn of this project.
- The engine and widget profiles additionally pin open-weight-only provider
  allowlists (e.g. morph, akashml, baidu, novita, siliconflow, coreweave).
  Fallback models are open-weight (`deepseek/deepseek-flash-latest`).
- **Human in the loop:** Shaun Xenon — direction, architecture decisions, the
  quality bar, and final acceptance. Commits carry his git identity;
  per-slice authorship lives in PR descriptions and this file (no synthetic
  co-author emails).
