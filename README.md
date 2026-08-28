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

Set `retry_attempts` and `retry_delay_seconds` globally or per stage to resume transient command failures automatically. A monitor can also invoke an explicitly configured Windows Scheduled Task with `--recovery-task`; use that only for a task you own which restarts the same bounded configuration.

The runner intentionally does not retry past `max_candidates`, activate a candidate, or delete old models. Review the persisted `monitor_state.json`, `automation_events.jsonl`, logs, and score files before any activation decision.

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
