---
name: training-job-monitor
description: Launch and supervise the single-instance Windows monitor for a long training, conversion, evaluation, or transfer.
---

# Training Job Monitor

Use `scripts/launch_training_monitor.ps1` after a long-running process has a PID or after the candidate launcher has created a run directory.

Pass the log path, artifact directory, state path, and process ID. The monitor is an observer: it reports status and can request a separately configured recovery task, but it never activates or deletes a model.

Use `-WhatIf` before first launch to validate paths. Keep the monitor open while a candidate loop runs; it shows live elapsed time and the latest log output every second.
