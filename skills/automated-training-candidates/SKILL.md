---
name: automated-training-candidates
description: Run a bounded, score-gated local or cloud training workflow through Training Candidate Automation.
---

# Automated Training Candidates

Use this skill when a training workflow must execute train, merge, transfer, evaluation, and bounded retraining candidates until every configured JSON score report is perfect.

Set `max_candidates` explicitly. Use argument arrays for every stage command. Keep model activation, manifest changes, deployment, deletion, and external publication outside the loop.

1. Create a configuration from `examples/score-gated-local.json` with real project paths and score report keys.
2. Validate the command shape with `scripts/launch_candidate_loop.ps1 -WhatIf`.
3. Start the launcher and let the loop own stage order.
4. Classify a terminal result from `monitor_state.json`: malformed scores, harness errors, and runtime failures are not model-quality evidence. For automatic bounded continuation, configure `cap_recovery` with `max_handoffs` and a command array; it receives `{scores_path}` and `{run_dir}` and must select and launch the next bounded configuration.
5. Activate a successful candidate only after a separate user-approved validation.
