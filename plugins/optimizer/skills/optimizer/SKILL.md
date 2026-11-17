---
name: optimizer
description: Optimize a measured target by running Driveline autoresearch on the ranked hypotheses of an EGM-reviewed design instead of ad-hoc ideas. Use for requests like "make X faster/smaller/better" when a benchmark can be run.
---

# Optimizer

This skill is the handoff between two tools. Driveline autoresearch sets up and calibrates a locked benchmark. EGM produces a reviewed design with ranked hypotheses grounded in that calibration. Driveline then runs measured experiments on them. Follow each tool's own skill for everything this page does not mention.

- Run the whole flow yourself unless initial inspection reveals an invalid evaluation (target or split leakage, broken scorer, failing baseline invariants, or compute the user did not authorize)—in that case stop before setup and report what to repair. Otherwise the user states the goal once and only reads results; do not ask them to type commands, answer EGM's questions or approve the design.
- EGM's skills (`egm-brainstorm`, `egm-draft`, `egm-review`, `egm-resolve`) set `disable-model-invocation: true`, so skill tools refuse or hide them. Read each `SKILL.md` directly and follow it in this thread, using that `skills/` folder's parent as the EGM plugin root: in Claude use `EGM skills (claude):` from `optimizer --help`; in `agy` use `~/.gemini/config/plugins/egm/skills` if present, else `EGM skills:`; in Codex use the installed `egm` plugin's `skills/` if listed, else `EGM skills:`. Dispatch Goldfish reviewers directly when synthesizer nesting is unavailable.
- Wherever an EGM skill waits for the user (clarifying questions, readiness, architecture and section gates, review findings, contradictions, final alignment), do not call question tools or request artifact feedback; decide it yourself from the user's request and the measured evidence, write all EGM drafts and review reports outside the experiment worktree (for example under the target's `docs/designs/` or your harness's artifact directory) so untracked files never dirty the worktree, and record each decision and its reason in the design. Run `egm-review` in its `uninterrupted` mode.
- `optimizer --help` prints the path of the pinned Driveline skill on its `Driveline skill:` line. Read that file before setup.

## 1. Measure and calibrate

1. Inspect the workload, data/splits, and scorer first; stop if the evaluation contract is invalid.
2. Follow Driveline Setup steps 1–5 in a dedicated experiment worktree on its own branch (`autoresearch.md`, `autoresearch.sh`, and `checks.sh` when needed), adding Driveline's session excludes plus local caches, venvs and any workload outputs (`/.venv/`, `/__pycache__/`, `/.pytest_cache/`, `/.uv-cache/`) to `.git/info/exclude`. Use `#!/usr/bin/env bash` in `autoresearch.sh`; before it starts the workload, snapshot tracked and non-ignored untracked changes to `experiments/candidate.patch` (so the exact measured code survives later edits) and print elapsed wall seconds when it ends:

   ```bash
   mkdir -p experiments
   { git diff --binary HEAD
     git ls-files -z --others --exclude-standard | while IFS= read -r -d '' f; do git diff --binary --no-index -- /dev/null "$f" || true; done
   } > experiments/candidate.patch
   ```

   `checks.sh` prints `CHECKS_OK`, or `CHECKS_FAILED (exit N)` on any non-zero exit and exits non-zero, so a failing check is logged `checks_failed`, never `discard`. Do not run `ar_state.py init` yet so EGM review time does not consume `maxSeconds`.
3. Measure where the metric comes from—stage timing, memory or allocation split after a warmup run for speed/size targets, or train-vs-validation gap, slice errors, and a simple baseline for quality targets—and verify with quick local probes that candidate knobs are live. Save this evidence outside the worktree.
4. Look for prior art: the target's `docs/designs/`, `git branch` and `git worktree list` may hold earlier ledgers or kept commits on the same code. Read their actual deltas, not summaries, and save the ones that apply with the evidence.
5. Run `checks.sh` once on the clean baseline (`T_checks`) and run Driveline's 3–5 baseline repeats (`T_bench`) to establish `baseline` and `noiseFloor` (when repeat `stdev` is near zero on a small fixed validation split, floor `noiseFloor` at fold SE or minimum practical gain and cap `maxRuns` tightly).

## 2. Design with EGM

1. Run the normal EGM flow (`brainstorm`, `draft`, `review`), passing the saved profile, calibration numbers, prior-art ledgers and scoped target source files in `GoldfishContext`. Reviewers check that each row's expected payoff does not exceed its measured stage time (and that payoffs do not sum past the baseline), and that rows do not depend on each other unless the table says so.
2. Include a `## Ranked hypotheses` table with `rank | id | mechanism` as the first three columns:

   `rank | id | mechanism | evidence | expected payoff | falsifier`

   and a `## Detailed Implementation Plan` mapping each `id` to its target files and interfaces so Stage 9 (`egm-codebase-details`) checks code-level dependencies while leaving patch details open across retries.
3. Order prerequisites before dependents, then highest expected payoff (bounded by measured stage time) against scope and risk, so localized or already-proven mechanisms (including prior-art ports) come before large rewrites. Omit any hypothesis whose expected payoff cannot exceed `noiseFloor`; bundle a zero-gain enabling prerequisite into the first consumer row that clears `noiseFloor`.
4. Once every review finding is resolved (re-verifying `checks.sh` on baseline if review added invariants), save the approved design outside the worktree, for example under `docs/designs/`, and note its absolute path.

## 3. Initialize and run

Immediately before launching, run `ar_state.py init` with the calibrated `noiseFloor` and budgets, then log the calibrated baseline as row 1 (`keep`) with `ar-log.sh`.

- Turn a relative goal into `targetMetric` from the measured baseline. "3x faster" with a 12 s baseline is `4`.
- Size from the measured `T_bench` and `T_checks`: `--turn-timeout` at least `3 × T_bench + T_checks + 180` s (minimum 300); `maxSeconds` at least planned turns × (`T_bench + T_checks`), so the run count fits the time; `maxRuns` = planned turns + 1 (the baseline row). Avoid `--effort low` when `T_bench + T_checks` is 180 s or more. Never exceed a user-given cap—clamp `maxRuns` and `--max-turns` down instead—and state any assumed budgets in the report.

```sh
optimizer --workspace <worktree> --reviewed-design <absolute design path> \
  --worker claude|agy|codex [--model MODEL] [--effort EFFORT] [--read-path PATH] \
  --max-turns N --max-seconds S --turn-timeout T --protect <scorer or data file>
```

- Every flag except `--worker`, `--effort`, `--read-path` and `--reviewed-design` is Driveline's own; `--dry-run` checks the setup without launching a worker.
- Use the worker the user names. Otherwise use the host you are running in, and keep the same worker when resuming.
- Each turn is a fresh worker that takes the highest-ranked hypothesis not yet settled in the ledger and worklog, and puts that id first in the result description. The report groups runs by that id.
- A long run outlasts a shell tool call. Start it in the background and poll `optimizer --workspace <worktree> --status` and `autoresearch-dashboard.md`.

## 4. When it pauses

The supervisor writes `.autoresearch-off` and prints the reason. Read the ledger, worklog and dashboard, then:

| Reason | Next step |
|---|---|
| `target reached` | Report. |
| `reviewed hypotheses exhausted` | If the goal is unmet: re-profile the current best, take the worklog's "Proposed for review" notes back through EGM review, update the ranked table, then resume. |
| turn, run or wall-time budget reached | The user decides whether to extend. Follow Driveline's resume rules; deleting the pause file does not extend a budget. |
| anything else (timeout, CLI failure, dirty workspace, locked file changed) | Read `experiments/codex/run-N-*/events.jsonl`, `experiments/candidate.patch`, `git status` and `git log` first. Never run `autoresearch.sh` on a dirty timed-out worktree, and never log `keep` unless `HEAD` holds the verified commit; restore uncommitted edits, log from the worker's recorded measurement (`discard`/`checks_failed`) or `crash` (`0`), then resume. |

## 5. Report

Run `optimizer report --workspace <worktree> --design <absolute design path>` and give the user the printed `report.html` path. It shows baseline to best, the metric per experiment, and attempts per reviewed hypothesis. Say plainly that each keep was measured by the worker under Driveline's noise-floor rule and was not independently re-measured. Pushing, opening a PR or deploying needs separate authorization.
