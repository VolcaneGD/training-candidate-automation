from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import candidate_loop
import training_monitor


class PublicPackageTests(unittest.TestCase):
    def test_dashboard_locale_provides_japanese_action_guidance(self) -> None:
        self.assertEqual(training_monitor.dashboard_text("ja", "live_log"), "ライブログ")
        self.assertIn("Codex", training_monitor.safety_cap_guidance("ja"))
        self.assertEqual(training_monitor.dashboard_font("ja"), "BIZ UDPGothic")

    def test_retry_wait_elapsed_counts_from_retry_start(self) -> None:
        self.assertEqual(training_monitor.stage_elapsed_seconds(
            {"phase": "stage_retry_wait", "retry_started_at": 100.0}, now=107.0,
        ), 7)

    def test_exhausted_stage_retry_is_stopped_with_action_guidance(self) -> None:
        state = {"phase": "failed", "reason": "stage_failed", "candidate": 9, "stage": "download"}
        self.assertEqual(training_monitor.dashboard_status_badge("failed", "completed")[0], "STOPPED")
        self.assertIsNone(training_monitor.recovery_request_key(state))
        self.assertIn("ACTION REQUIRED", training_monitor.completion_log_summary(state))

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


if __name__ == "__main__":
    unittest.main()
