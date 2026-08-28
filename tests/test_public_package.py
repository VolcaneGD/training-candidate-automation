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

    def test_monitor_uses_generic_single_instance_namespace(self) -> None:
        self.assertTrue(training_monitor.singleton_mutex_name("demo").startswith("Local\\TrainingCandidateMonitor-"))

    def test_cap_recovery_handoff_receives_score_summary_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            commands: list[list[str]] = []

            def runner(command: list[str], _workdir: Path, _log_path: Path) -> int:
                commands.append(command)
                if command[0] == "evaluate":
                    (root / "scores").mkdir(exist_ok=True)
                    (root / "scores" / "demo-v1.json").write_text(
                        json.dumps({"passed": 1, "cases": 2}), encoding="utf-8"
                    )
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
