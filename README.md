# optimizer

Run [Driveline autoresearch](vendor/driveline-autoresearch/README.md) on hypotheses from an [EGM](vendor/elephant-goldfish-model/README.md)-reviewed design, so experiments follow a reviewed, ranked list instead of ad-hoc ideas.

```text
Driveline worktree: validate contract → profile & calibrate baseline / noiseFloor
                                   ↓
EGM: brainstorm → draft → uninterrupted Goldfish review → ranked hypotheses
                                   ↓
Driveline: init state → optimizer --reviewed-design … → one measured experiment per turn
                                   ↓
pause (target reached, hypotheses exhausted, budget) → report, or back to EGM review
```

This repository is the seam only:

| Piece | What it does |
|---|---|
| `patches/driveline-reviewed-design.patch` | Adds `--reviewed-design PATH` to Driveline's supervisor: each turn takes the highest-ranked open hypothesis, and the loop pauses when none are left. Already applied to `vendor/driveline-autoresearch`. |
| `scripts/optimizer_worker.py` | The `optimizer` command (`optimizer`, `optimizer install`, `optimizer report`). Runs Driveline's supervisor with a Codex, Claude or agy worker; every other flag is Driveline's. |
| `scripts/optimizer_report.py` | `optimizer report`: renders Driveline's ledger as `report.html` and `report.md`, grouped by reviewed hypothesis. Read-only. |
| `plugins/optimizer/` | The agent skill describing the handoff. |
| `vendor/`, `upstreams.lock.json` | Pinned upstream copies with hashes and licenses. |

Keeps are measured by the worker under Driveline's noise-floor rule. Nothing here re-measures them.

## Install

Requires Python 3.12+, Git, uv, macOS or Linux, and an authenticated `codex`, `claude` or `agy` CLI.

From Git:

```sh
uv tool install git+https://github.com/vedatonuryilmaz/dev_optimizer
optimizer install
```

Or from a local checkout:

```sh
git clone https://github.com/vedatonuryilmaz/dev_optimizer
uv tool install --editable ./dev_optimizer
optimizer install
```

`optimizer install` sets up both the `optimizer` skill and the pinned vendored EGM plugin for every installed host CLI on `PATH` (`claude`, `codex`, `agy`), or a specific host with `optimizer install --host claude|codex|agy`. Re-run `optimizer install` after upgrading (or after editing the skill for `agy`; Claude Code and Codex use symlinks). Driveline needs no separate install: `optimizer` runs the vendored supervisor and `optimizer --help` prints the Driveline and EGM skill paths.

Start a new agent session afterwards.

## Use

In a target repository, ask your agent (`claude`, `codex` or `agy`) to optimize something measurable, for example "make the parser 3x faster, up to 100 experiments in 8 hours, use claude sonnet". The agent runs the whole flow hands-off—contract check, harness profiling and noise calibration, EGM design and uninterrupted Goldfish review, measured experiments, and the final report—following the [skill](plugins/optimizer/skills/optimizer/SKILL.md).

To run the supervisor yourself in an initialized Driveline worktree:

```sh
optimizer --workspace <worktree> --reviewed-design <design.md> --worker claude --max-turns 20 --max-seconds 7200
optimizer --workspace <worktree> --status
optimizer report --workspace <worktree> --design <design.md>   # writes <worktree>/experiments/report.html
```

## Develop

```sh
uv run pytest -q
```

Changing vendored bytes requires regenerating the patch and its hashes in `upstreams.lock.json`; `tests/test_upstream_contract.py` checks both.

## License

Licensed under either of [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option.

Vendored upstreams keep their own licenses: [Driveline autoresearch](https://github.com/drivelineresearch/autoresearch-claude-code) is MIT ([`vendor/driveline-autoresearch/LICENSE`](vendor/driveline-autoresearch/LICENSE)) and is modified by `patches/driveline-reviewed-design.patch`; [EGM](https://github.com/google/elephant-goldfish-model) is Apache-2.0 ([`vendor/elephant-goldfish-model/LICENSE`](vendor/elephant-goldfish-model/LICENSE)) and is unmodified. Pinned revisions and hashes are in `upstreams.lock.json`.
