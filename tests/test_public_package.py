from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import candidate_loop
import monitor_watchdog
import training_monitor


class PublicPackageTests(unittest.TestCase):
    def test_dashboard_locale_provides_japanese_action_guidance(self) -> None:
        self.assertEqual(training_monitor.dashboard_text("ja", "live_log"), "ライブログ")
        self.assertIn("Codex", training_monitor.safety_cap_guidance("ja"))
        self.assertEqual(training_monitor.dashboard_font("ja"), "Meiryo UI")

    def test_dashboard_locale_translates_metric_and_stage_labels(self) -> None:
        self.assertEqual(training_monitor.dashboard_text("ja", "candidate"), "現候補")
        self.assertEqual(training_monitor.dashboard_text("ja", "artifacts"), "アーティファクト")
        self.assertEqual(training_monitor.dashboard_stage_label("modal_train", "ja"), "Modal で学習")

    def test_japanese_stage_metric_wraps_with_a_smaller_font(self) -> None:
        self.assertEqual(
            training_monitor.dashboard_metric_value_options("stage", "ja"),
            {"font": ("Meiryo UI", 8), "wraplength": 118},
        )
        self.assertEqual(
            training_monitor.dashboard_metric_label_options("artifacts", "ja"),
            {"font": ("Meiryo UI", 8), "wraplength": 118},
        )

    def test_retry_wait_elapsed_counts_from_retry_start(self) -> None:
        self.assertEqual(training_monitor.stage_elapsed_seconds(
            {"phase": "stage_retry_wait", "retry_started_at": 100.0}, now=107.0,
        ), 7)

    def test_monitoring_elapsed_uses_the_candidate_run_start(self) -> None:
        self.assertEqual(
            training_monitor.run_elapsed_seconds({"run_started_at": 100.0}, now=167.0),
            67,
        )

    def test_windows_process_probe_never_opens_tasklist_console(self) -> None:
        completed = mock.Mock(stdout="python.exe                    42 Console")
        with mock.patch.object(training_monitor.subprocess, "run", return_value=completed) as run:
            self.assertTrue(training_monitor.process_exists(42))

        self.assertEqual(
            run.call_args.kwargs["creationflags"],
            getattr(training_monitor.subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_refresh_delay_compensates_for_snapshot_work(self) -> None:
        self.assertEqual(
            training_monitor.next_refresh_delay_ms(1, refresh_started_at=100.0, now=100.25),
            750,
        )

    def test_monitor_render_heartbeat_becomes_stale_after_five_seconds(self) -> None:
        self.assertFalse(training_monitor.monitor_heartbeat_stale(100.0, now=104.9))
        self.assertTrue(training_monitor.monitor_heartbeat_stale(100.0, now=105.0))

    def test_existing_event_log_supplies_a_legacy_run_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_path = Path(temp_dir) / "automation_events.jsonl"
            event_path.write_text(json.dumps({"stage_started_at": 123.0}) + "\n", encoding="utf-8")
            self.assertEqual(training_monitor.run_started_at_from_events(event_path), 123.0)

    def test_exhausted_stage_retry_is_stopped_with_action_guidance(self) -> None:
        state = {"phase": "failed", "reason": "stage_failed", "candidate": 9, "stage": "download"}
        self.assertEqual(training_monitor.dashboard_status_badge("failed", "completed")[0], "STOPPED")
        self.assertIsNone(training_monitor.recovery_request_key(state))
        self.assertIn("ACTION REQUIRED", training_monitor.completion_log_summary(state))

    def test_regression_stop_includes_action_guidance(self) -> None:
        summary = training_monitor.completion_log_summary({
            "phase": "failed", "reason": "regression_detected", "candidate": 10,
        })
        self.assertIn("ACTION REQUIRED", summary)
        self.assertIn("regression", summary.lower())

    def test_regression_gate_is_stopped_not_retried_when_recovery_task_is_configured(self) -> None:
        state = {"phase": "failed", "reason": "regression_detected", "candidate": 10}
        self.assertIsNone(training_monitor.recovery_request_key(state))
        self.assertEqual(
            training_monitor.recovery_status_badge(
                "failed", "failed", reason="regression_detected",
                recovery_task="Example", request_started_at=0.0, request_accepted=True,
            ),
            ("STOPPED", "#fb7185", False),
        )

    def test_stop_report_contains_copy_ready_failure_details(self) -> None:
        report = training_monitor.stop_report_text({
            "phase": "failed", "reason": "stage_failed", "candidate": 9,
            "stage": "download_merged", "log_path": r"D:\\run\\stage.log",
            "scores": [{"path": r"D:\\scores\\fixed.json", "passed": 7, "cases": 8}],
        })
        self.assertIn("Candidate: 9", report)
        self.assertIn("Reason: stage_failed", report)
        self.assertIn("Stage: download_merged", report)
        self.assertIn(r"D:\\run\\stage.log", report)
        self.assertIn("fixed: 7/8", report)
        self.assertTrue(report.startswith("```text\n"))
        self.assertTrue(report.endswith("\n```"))

    def test_dead_process_overrides_stale_running_phase(self) -> None:
        self.assertEqual(
            training_monitor.dashboard_status_badge("stage_running", "completed"),
            ("STOPPED", "#fb7185", False),
        )

    def test_dead_stage_process_marks_active_state_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "monitor_state.json"
            state_path.write_text(json.dumps({
                "phase": "stage_running", "loop_process_id": 10,
                "command_process_id": 11, "heartbeat_at": 100.0,
            }), encoding="utf-8")
            snapshot = training_monitor.build_snapshot(
                watch_path=None, log_path=None, process_id=10, modal_app=None,
                state_path=state_path, process_exists=lambda pid: pid == 10,
            )
        self.assertEqual(snapshot["overall_state"], "failed")
        self.assertEqual(snapshot["liveness_reason"], "stage_process_exited")

    def test_score_gated_run_completes_with_a_generic_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            score_path = root / "scores" / "demo-v1.json"

            def runner(command: list[str], _workdir: Path, _log_path: Path) -> int:
                if command[0] == "evaluate":
                    score_path.parent.mkdir(parents=True, exist_ok=True)
                    score_path.write_text(json.dumps({"passed": 2, "cases": 2}), encoding="utf-8")
                return 0

            result = candidate_loop.run_loop(
                {
                    "max_candidates": 1,
                    "release_template": "demo-v{candidate}",
                    "workdir": str(root),
                    "stages": [
                        {"name": "train", "command": ["train", "{release_name}"]},
                        {"name": "evaluate", "command": ["evaluate", "{release_name}"]},
                    ],
                    "score_reports": [{"path": "scores/{release_name}.json"}],
                },
                run_dir=root / "run",
                command_runner=runner,
                notifier=lambda *_: None,
            )

        self.assertTrue(result["perfect"])
        self.assertEqual(result["candidate"], 1)

    def test_cleanup_removes_only_old_rejected_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for candidate, perfect in ((1, False), (2, False), (3, True)):
                run = root / f"v{candidate}-candidate" / "run"
                artifacts = root / f"v{candidate}-candidate" / "artifacts" / f"demo-v{candidate}"
                run.mkdir(parents=True)
                artifacts.mkdir(parents=True)
                (artifacts / "model.gguf").write_text("model", encoding="utf-8")
                (run / "monitor_state.json").write_text(
                    json.dumps({"phase": "perfect_score" if perfect else "failed", "perfect": perfect, "candidate": candidate}),
                    encoding="utf-8",
                )

            removed = candidate_loop.cleanup_rejected_artifacts({"artifact_cleanup": {
                "enabled": True,
                "retain_latest_candidates": 2,
                "automation_root": str(root),
            }
            })

            self.assertEqual([path.name for path in removed], ["demo-v1"])
            self.assertFalse((root / "v1-candidate" / "artifacts" / "demo-v1").exists())
            self.assertTrue((root / "v2-candidate" / "artifacts" / "demo-v2").exists())
            self.assertTrue((root / "v3-candidate" / "artifacts" / "demo-v3").exists())

    def test_monitor_uses_generic_single_instance_namespace(self) -> None:
        self.assertTrue(training_monitor.singleton_mutex_name("demo").startswith("Local\\TrainingCandidateMonitor-"))

    def test_terminal_summary_uses_the_stopped_status_color(self) -> None:
        self.assertEqual(
            training_monitor.completion_summary_color({"phase": "failed"}),
            "#fb7185",
        )

    def test_unconfirmed_recovery_remains_retrying_for_automatic_restart(self) -> None:
        self.assertEqual(
            training_monitor.recovery_status_badge(
                "failed", "failed", recovery_task="Example", request_started_at=0.0,
                request_accepted=True, now=training_monitor.RECOVERY_CONFIRMATION_SECONDS + 1,
            ),
            ("RETRYING", "#fbbf24", False),
        )

    def test_recovery_request_is_reissued_after_the_retry_interval(self) -> None:
        self.assertTrue(
            training_monitor.recovery_request_due(
                0.0, now=training_monitor.RECOVERY_CONFIRMATION_SECONDS,
            )
        )

    def test_candidate_cap_is_stopped_instead_of_retrying(self) -> None:
        self.assertIsNone(training_monitor.recovery_request_key({
            "phase": "failed", "reason": "cap_recovery_failed", "candidate": 3,
        }))
        self.assertIn(
            "ACTION REQUIRED",
            training_monitor.completion_log_summary({
                "phase": "failed", "reason": "cap_recovery_failed", "candidate": 3,
            }),
        )

    def test_cap_recovery_handoff_receives_score_summary_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            commands: list[list[str]] = []
            recovery_states: list[dict[str, object]] = []

            def runner(command: list[str], _workdir: Path, _log_path: Path) -> int:
                commands.append(command)
                if command[0] == "evaluate":
                    (root / "scores").mkdir(exist_ok=True)
                    (root / "scores" / "demo-v1.json").write_text(
                        json.dumps({"passed": 1, "cases": 2}), encoding="utf-8"
                    )
                if command[0] == "recover":
                    recovery_states.append(json.loads((root / "run" / "monitor_state.json").read_text(encoding="utf-8")))
                return 0

            result = candidate_loop.run_loop(
                {
                    "max_candidates": 1,
                    "release_template": "demo-v{candidate}",
                    "workdir": str(root),
                    "stages": [{"name": "evaluate", "command": ["evaluate"]}],
                    "score_reports": [{"path": "scores/{release_name}.json"}],
                    "cap_recovery": {
                        "max_handoffs": 1,
                        "command": ["recover", "{scores_path}", "{run_dir}", "{candidate}"],
                    },
                },
                run_dir=root / "run",
                command_runner=runner,
                notifier=lambda *_: None,
            )

            self.assertEqual(result["reason"], "cap_recovery_started")
            self.assertEqual(commands[-1][0], "recover")
            summary = Path(commands[-1][1])
            self.assertEqual(json.loads(summary.read_text(encoding="utf-8"))["scores"][0]["passed"], 1)
            self.assertEqual(commands[-1][2], str(root / "run"))
            self.assertEqual(commands[-1][3], "1")
            self.assertEqual(recovery_states[0]["reason"], "candidate_cap_reached")
            self.assertEqual(recovery_states[0]["scores"][0]["passed"], 1)

    def test_regression_guard_stops_candidate_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            commands: list[list[str]] = []

            def runner(command: list[str], _workdir: Path, _log_path: Path) -> int:
                commands.append(command)
                (root / "score-1.json").write_text('{"passed": 1, "cases": 2}', encoding="utf-8")
                return 0

            result = candidate_loop.run_loop(
                {
                    "max_candidates": 1,
                    "release_template": "candidate-{candidate}",
                    "workdir": str(root),
                    "stages": [{"name": "evaluate", "command": ["evaluate"]}],
                    "score_reports": [{"path": "score-{candidate}.json"}],
                    "regression_guards": [{"path": "score-{candidate}.json", "minimum_passed": 2}],
                    "cap_recovery": {"max_handoffs": 1, "command": ["must-not-run"]},
                },
                run_dir=root / "run",
                command_runner=runner,
                notifier=lambda *_: None,
            )
            state = json.loads((root / "run" / "monitor_state.json").read_text(encoding="utf-8"))

        self.assertEqual(result["reason"], "regression_detected")
        self.assertEqual(result["regressions"][0]["minimum_passed"], 2)
        self.assertEqual(state["phase"], "failed")
        self.assertNotIn(["must-not-run"], commands)

    def test_regression_guard_distinguishes_dotted_scores_in_one_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = {
                "max_candidates": 1,
                "release_template": "candidate-{candidate}",
                "workdir": str(root),
                "stages": [{"name": "evaluate", "command": ["evaluate"]}],
                "score_reports": [
                    {"path": "runtime-{candidate}.json", "passed_key": "summary.primary.passed", "cases_key": "summary.primary.cases"},
                    {"path": "runtime-{candidate}.json", "passed_key": "summary.secondary.passed", "cases_key": "summary.secondary.cases"},
                ],
                "regression_guards": [
                    {"path": "runtime-{candidate}.json", "passed_key": "summary.primary.passed", "minimum_passed": 3},
                    {"path": "runtime-{candidate}.json", "passed_key": "summary.secondary.passed", "minimum_passed": 1},
                ],
            }

            def runner(_command: list[str], _workdir: Path, _log_path: Path) -> int:
                (root / "runtime-1.json").write_text('{"summary":{"primary":{"passed":3,"cases":3},"secondary":{"passed":1,"cases":1}}}', encoding="utf-8")
                return 0

            result = candidate_loop.run_loop(config, run_dir=root / "run", command_runner=runner, notifier=lambda *_: None)

        self.assertTrue(result["perfect"])

    def test_repair_receives_next_stronger_curriculum_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            commands: list[list[str]] = []

            def runner(command: list[str], _workdir: Path, _log_path: Path) -> int:
                commands.append(command)
                if command[0] == "evaluate":
                    (root / f"score-{command[1]}.json").write_text(
                        '{"passed": 1, "cases": 2}', encoding="utf-8"
                    )
                return 0

            result = candidate_loop.run_loop(
                {
                    "max_candidates": 2,
                    "first_candidate": 4,
                    "release_template": "candidate-{candidate}",
                    "curriculum_template": "curriculum.v{curriculum_version}.jsonl",
                    "initial_curriculum_version": 12,
                    "workdir": str(root),
                    "stages": [{"name": "evaluate", "command": ["evaluate", "{candidate}"]}],
                    "score_reports": [{"path": "score-{candidate}.json"}],
                    "repair_commands": [[
                        "repair", "{curriculum_name}", "{next_curriculum_name}",
                        "{scores_path}", "{next_curriculum_version}",
                    ]],
                },
                run_dir=root / "run",
                command_runner=runner,
                notifier=lambda *_: None,
            )

        self.assertEqual(result["reason"], "candidate_cap_reached")
        self.assertEqual(
            commands[1][:5],
            ["repair", "curriculum.v12.jsonl", "curriculum.v13.jsonl", str(root / "run" / "candidate-004-scores.json"), "13"],
        )

    def test_experiment_ledger_records_curriculum_adapter_and_preference_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "experiments.jsonl"
            candidate_loop.append_experiment_ledger(path, {
                "candidate": 13, "release_name": "executor-v13", "curriculum_version": 13,
                "curriculum_name": "executor.v13.jsonl", "resume_adapter_name": "executor-v12",
                "training_mode": "dpo", "scores": [{"passed": 8, "cases": 8}],
            })
            row = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(row["candidate"], 13)
        self.assertEqual(row["curriculum"]["version"], 13)
        self.assertEqual(row["adapter"]["resume"], "executor-v12")
        self.assertEqual(row["training_mode"], "dpo")

    def test_monitor_launcher_forwards_an_explicit_recovery_task(self) -> None:
        launcher = ROOT / "scripts" / "launch_training_monitor.ps1"
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher),
                "-RecoveryTask", "Example Training Recovery", "-WhatIf",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--recovery-task", result.stdout)
        self.assertIn("Example Training Recovery", result.stdout)

    def test_candidate_launcher_replaces_a_stale_monitor(self) -> None:
        launcher = ROOT / "scripts" / "launch_candidate_loop.ps1"
        source = launcher.read_text(encoding="utf-8")
        self.assertIn("$monitorArgs.ReplaceExisting = $true", source)
        monitor_source = (ROOT / "scripts" / "launch_training_monitor.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-CimInstance Win32_Process", monitor_source)
        self.assertIn("Stop-Process", monitor_source)

    def test_stage_commands_are_started_without_a_console_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "stage.log"
            with mock.patch.object(candidate_loop.subprocess, "Popen") as popen:
                popen.return_value.poll.return_value = 0
                popen.return_value.returncode = 0
                result = candidate_loop._command_runner(["modal", "run"], Path(temp_dir), log_path)

        self.assertEqual(result, 0)
        self.assertEqual(
            popen.call_args.kwargs["creationflags"],
            getattr(candidate_loop.subprocess, "CREATE_NO_WINDOW", 0),
        )

        startupinfo = popen.call_args.kwargs["startupinfo"]
        self.assertEqual(
            startupinfo.dwFlags & candidate_loop.subprocess.STARTF_USESHOWWINDOW,
            candidate_loop.subprocess.STARTF_USESHOWWINDOW,
        )
        self.assertEqual(startupinfo.wShowWindow, candidate_loop.subprocess.SW_HIDE)

    def test_stage_command_uses_pythonw_when_available(self) -> None:
        actual = candidate_loop.windowless_command(
            [r"D:\MicroCodeTraining\.venv-pytorch212b\Scripts\python.exe", "-m", "modal"],
            executable_exists=lambda _path: True,
        )

        self.assertEqual(actual[0], r"D:\MicroCodeTraining\.venv-pytorch212b\Scripts\pythonw.exe")

    def test_watchdog_relaunch_preserves_its_own_process(self) -> None:
        arguments = __import__("argparse").Namespace(
            launcher=Path(r"C:\\tools\\launch_training_monitor.ps1"),
            title="Test job", watch_path=r"D:\\run", log_path=r"D:\\run\\automation.log",
            state_path=Path(r"D:\\run\\monitor_state.json"), process_id=42,
            instance_key="test-monitor",
        )

        command = monitor_watchdog.recovery_launcher_command(arguments)

        self.assertIn("-KeepWatchdog", command)
        self.assertIn("-ReplaceExisting", command)


if __name__ == "__main__":
    unittest.main()
