# Training Candidate Automation

MIT-licensed Windows automation for bounded model-training candidates. It runs configured command-array stages, records live state and logs, checks score reports, retries failed stages, runs optional repair commands between candidates, and stops at a mandatory safety cap.

It is model- and provider-agnostic: use it with local commands, cloud CLIs, or any training framework that can write JSON scores. It never activates a model, changes a manifest, uploads credentials, or deletes artifacts.

## Install

```powershell
git clone https://github.com/VolcaneGD/training-candidate-automation.git
cd training-candidate-automation
python -m pip install -e .
```

No third-party Python package is required. Windows monitoring uses the standard-library Tkinter UI.

## Configure a bounded run

Copy [examples/score-gated-local.json](examples/score-gated-local.json) and replace every command and path with values for your project.

`max_candidates` is mandatory. Each stage uses an argument array, never a shell command string. A score report must contain integer `passed` and `cases` values; dotted keys such as `summary.passed` are supported.

To strengthen a curriculum after each valid miss instead of repeating the same
dataset, configure a versioned template. Each `repair_commands` entry receives
the current and next curriculum names plus a JSON score summary.

```json
{
  "curriculum_template": "datasets/executor-repair.v{curriculum_version}.jsonl",
  "initial_curriculum_version": 13,
  "repair_commands": [[
    "python", "build_repair_curriculum.py",
    "--scores-path", "{scores_path}",
    "--curriculum-version", "{next_curriculum_version}",
    "--output", "{next_curriculum_name}"
  ]]
}
```

The runner exposes `{curriculum_version}`, `{curriculum_name}`,
`{next_curriculum_version}`, `{next_curriculum_name}`, and `{scores_path}`.
Malformed reports and protected-score regressions stop before a repair command
can run, so an invalid measurement never becomes training data.

Every scored candidate is also appended to `experiment_ledger.jsonl` in the
run directory. Set `experiment_ledger_path` to move it. Ledger rows retain the
candidate, curriculum version, resumed/combined adapter identifier, training
mode (`sft` or `dpo`), and score summaries, but never copy prompts or training
records. Use `adapter_composition` to label a PEFT weighted-adapter experiment;
the actual merge remains an explicit stage command and is evaluated by the same
score and regression gates.

```powershell
python candidate_loop.py --config C:\runs\candidate-config.json --run-dir C:\runs\candidate-automation
```

On Windows, use the launcher to run the loop hidden and open one monitor for its entire lifetime:

```powershell
& .\scripts\launch_candidate_loop.ps1 `
  -ConfigPath C:\runs\candidate-config.json `
  -RunDir C:\runs\candidate-automation `
  -Title 'My training candidates'
```

## Lifecycle

```text
candidate N -> configured stages -> score reports
     |             |                   |
     |         stage retry          all perfect -> COMPLETE
     |             |
     +--- below target -> repair_commands -> candidate N+1
                                      |
                              max_candidates -> STOPPED
```

The monitor refreshes every second. `RUNNING` pulses, `RETRYING` is yellow, `STOPPED` is red, and `COMPLETE` is green. The live log includes stage progress, elapsed time, failures, and the final score summary. It closes five seconds after a successful completion.

## Safe recovery

## Rejected artifact cleanup

Enable cleanup only after confirming that old candidate models are not needed for rollback. TCA deletes only `artifacts` subdirectories from terminal candidates with `perfect: false`; it preserves their logs, scores, configuration, and the newest rejected candidates.

```json
"artifact_cleanup": {
  "enabled": true,
  "automation_root": "D:\\temporary_file\\training-automation",
  "retain_latest_candidates": 2
}
```

`retain_latest_candidates` must be a positive integer. Cleanup is recorded in `automation_events.jsonl`.

Set `retry_attempts` and `retry_delay_seconds` globally or per stage to resume transient command failures automatically. A monitor can also invoke an explicitly configured Windows Scheduled Task with `--recovery-task`; use that only for a task you own which restarts the same bounded configuration. While a terminal state remains unchanged, the monitor reissues that recovery request every 30 seconds and keeps `RETRYING` visible with its attempt count in the LIVE LOG; it does not silently settle on `STOPPED` while automatic recovery is configured.

The runner never retries past `max_candidates` implicitly, activates a candidate, or deletes old models. For fully automated but still bounded continuation, configure `cap_recovery` with a positive `max_handoffs` and a command array. On a cap miss, TCA writes `cap_recovery_scores.json`, invokes that command with `{scores_path}`, `{run_dir}`, `{candidate}`, and `{release_name}`, and records `cap_recovery_started`. The recovery command owns domain-specific error classification and must launch the next bounded configuration; once `max_handoffs` is exhausted, TCA stays at `candidate_cap_reached`.

Use `regression_guards` to protect scores that were already passing. A guard compares a configured score path with `minimum_passed`; a below-baseline result ends the run as `regression_detected`, writes the scores to the monitor state, and bypasses `repair_commands` and `cap_recovery`. This prevents a newly targeted repair from silently replacing a broader successful candidate.

```json
"regression_guards": [
  {"path": "scores/{release_name}.boundary.json", "minimum_passed": 6},
  {"path": "scores/{release_name}.runtime.json", "minimum_passed": 3}
]
```

```json
"cap_recovery": {
  "max_handoffs": 2,
  "command": ["python", "recover.py", "--scores", "{scores_path}", "--previous-run", "{run_dir}"]
}
```

Review the persisted `monitor_state.json`, `automation_events.jsonl`, recovery log, and score files before any activation decision.

## Codex Skills

Copy the folders under `skills/` into your Codex skills directory, or use them as project-local skills. They guide Codex to create a safe command-array configuration, start the monitor, and classify failed measurements before proposing retraining.

## Tests

```powershell
python -m unittest discover -s tests -p 'test_*.py'
python candidate_loop.py --help
python training_monitor.py --help
& .\scripts\launch_candidate_loop.ps1 -ConfigPath C:\runs\candidate-config.json -RunDir C:\runs\candidate-automation -WhatIf
```

## Security

Do not commit model weights, datasets, logs, `.env` files, cloud tokens, or real project paths. The included `.gitignore` excludes common training artifacts, but inspect `git status` before publishing.
