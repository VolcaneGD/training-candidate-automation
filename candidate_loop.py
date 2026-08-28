"""Run configured training candidates until every score report is perfect or a cap is reached."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from training_monitor import send_notification


CommandRunner = Callable[[list[str], Path, Path], int]
Notifier = Callable[[str, str], None]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    if path.name == "monitor_state.json":
        value = {**value, "loop_process_id": os.getpid()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_event(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as events:
        events.write(json.dumps(value, ensure_ascii=False) + "\n")


def _render(value: str, context: dict[str, object]) -> str:
    try:
        return value.format(**context)
    except KeyError as error:
        raise ValueError(f"unknown template field: {error.args[0]}") from error


def _command_runner(command: list[str], workdir: Path, log_path: Path) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + subprocess.list2cmdline(command) + "\n\n")
        log.flush()
        return subprocess.run(command, cwd=workdir, stdout=log, stderr=subprocess.STDOUT, check=False).returncode


def _dotted_value(payload: object, key: str) -> object:
    value = payload
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(key)
        value = value[part]
    return value


def _read_scores(reports: list[object], context: dict[str, object], workdir: Path) -> tuple[bool, list[dict[str, object]]]:
    results: list[dict[str, object]] = []
    for raw in reports:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise ValueError("each score_reports item needs a path")
        path = Path(_render(raw["path"], context))
        if not path.is_absolute():
            path = workdir / path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            passed = _dotted_value(payload, str(raw.get("passed_key", "passed")))
            cases = _dotted_value(payload, str(raw.get("cases_key", "cases")))
            if not isinstance(passed, int) or not isinstance(cases, int):
                raise ValueError("score values must be integers")
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as error:
            raise ValueError(f"invalid score report {path}: {error}") from error
        results.append({"path": str(path), "passed": passed, "cases": cases, "perfect": cases > 0 and passed == cases})
    return all(bool(result["perfect"]) for result in results), results


def _validate_config(config: dict[str, object]) -> tuple[int, int, str, str, Path, list[object], list[object]]:
    maximum = config.get("max_candidates")
    template = config.get("release_template")
    workdir = config.get("workdir")
    stages = config.get("stages")
    reports = config.get("score_reports")
    first_candidate = config.get("first_candidate", 1)
    initial_resume_adapter = config.get("initial_resume_adapter", "")
    if not isinstance(maximum, int) or maximum < 1:
        raise ValueError("max_candidates must be a positive integer")
    if not isinstance(first_candidate, int) or first_candidate < 1:
        raise ValueError("first_candidate must be a positive integer")
    if not isinstance(initial_resume_adapter, str):
        raise ValueError("initial_resume_adapter must be a string")
    if not isinstance(template, str) or not template:
        raise ValueError("release_template must be a non-empty string")
    if not isinstance(workdir, str) or not workdir:
        raise ValueError("workdir must be a non-empty path")
    if not isinstance(stages, list) or not stages:
        raise ValueError("stages must be a non-empty list")
    if not isinstance(reports, list) or not reports:
        raise ValueError("score_reports must be a non-empty list")
    return maximum, first_candidate, initial_resume_adapter, template, Path(workdir), stages, reports


def _cap_recovery(config: dict[str, object]) -> tuple[int, list[object]] | None:
    raw = config.get("cap_recovery")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("cap_recovery must be an object")
    maximum = raw.get("max_handoffs")
    command = raw.get("command")
    if not isinstance(maximum, int) or maximum < 1:
        raise ValueError("cap_recovery.max_handoffs must be a positive integer")
    if not isinstance(command, list) or not command:
        raise ValueError("cap_recovery.command must be a non-empty command array")
    return maximum, command


def cleanup_rejected_artifacts(config: dict[str, object]) -> list[Path]:
    """Delete only old, terminal, rejected candidate artifact directories."""
    raw = config.get("artifact_cleanup")
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return []
    retained = raw.get("retain_latest_candidates", 2)
    root_value = raw.get("automation_root")
    if not isinstance(retained, int) or retained < 1:
        raise ValueError("artifact_cleanup.retain_latest_candidates must be a positive integer")
    if not isinstance(root_value, str) or not root_value:
        return []
    root = Path(root_value)
    candidates: list[tuple[int, Path, bool]] = []
    for directory in root.glob("v*-*"):
        match = re.match(r"v(\d+)-", directory.name)
        state_path = directory / "run" / "monitor_state.json"
        if not match or not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        perfect = state.get("perfect")
        if isinstance(perfect, bool):
            candidates.append((int(match.group(1)), directory, perfect))
    candidates.sort(key=lambda item: item[0])
    protected = {number for number, _directory, _perfect in candidates[-retained:]}
    removed: list[Path] = []
    for number, directory, perfect in candidates:
        if number in protected or perfect:
            continue
        for artifact in (directory / "artifacts").iterdir() if (directory / "artifacts").is_dir() else ():
            if artifact.is_dir():
                shutil.rmtree(artifact)
                removed.append(artifact)
    return removed


def run_loop(
    config: dict[str, object],
    *,
    command_runner: CommandRunner = _command_runner,
    notifier: Notifier = send_notification,
    run_dir: Path | None = None,
) -> dict[str, object]:
    maximum, first_candidate, initial_resume_adapter, template, workdir, stages, reports = _validate_config(config)
    if not workdir.is_dir():
        raise ValueError(f"workdir does not exist: {workdir}")
    output_dir = run_dir or Path(str(config.get("run_dir") or workdir / ".training-automation"))
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "monitor_state.json"
    events_path = output_dir / "automation_events.jsonl"
    repair_commands = config.get("repair_commands", [])
    if not isinstance(repair_commands, list):
        raise ValueError("repair_commands must be a list")
    prepare_directories = config.get("prepare_directories", [])
    if not isinstance(prepare_directories, list) or not all(isinstance(item, str) for item in prepare_directories):
        raise ValueError("prepare_directories must be a list of paths")
    cap_recovery = _cap_recovery(config)

    previous_release_name = initial_resume_adapter
    for candidate in range(first_candidate, first_candidate + maximum):
        release_name = _render(template, {"candidate": candidate})
        context: dict[str, object] = {
            "candidate": candidate,
            "release_name": release_name,
            "resume_adapter_name": previous_release_name,
            "workdir": str(workdir),
        }
        for raw_path in prepare_directories:
            directory = Path(_render(raw_path, context))
            if not directory.is_absolute():
                directory = workdir / directory
            directory.mkdir(parents=True, exist_ok=True)
        _write_json(state_path, {"phase": "candidate_started", "candidate": candidate, "release_name": release_name})
        for raw_stage in stages:
            if not isinstance(raw_stage, dict) or not isinstance(raw_stage.get("name"), str) or not isinstance(raw_stage.get("command"), list):
                raise ValueError("each stage needs a name and command array")
            name = raw_stage["name"]
            reuse_value = raw_stage.get("skip_if_exists")
            if reuse_value is not None and not isinstance(reuse_value, str):
                raise ValueError("stage skip_if_exists must be a path string")
            completion_value = raw_stage.get("completion_marker")
            if completion_value is not None and not isinstance(completion_value, str):
                raise ValueError("stage completion_marker must be a path string")
            if reuse_value:
                reuse_path = Path(_render(reuse_value, context))
                if not reuse_path.is_absolute():
                    reuse_path = workdir / reuse_path
                if reuse_path.exists():
                    completed = {
                        "event": "stage_completed",
                        "candidate": candidate,
                        "release_name": release_name,
                        "stage": name,
                        "reused": True,
                        "reused_path": str(reuse_path),
                        "stage_elapsed_seconds": 0,
                        "completed_at": time.time(),
                    }
                    _write_json(state_path, {"phase": "stage_completed", **completed})
                    _append_event(events_path, completed)
                    continue
            command = [_render(str(part), context) for part in raw_stage["command"]]
            retry_attempts = raw_stage.get("retry_attempts", 2)
            retry_delay_seconds = raw_stage.get("retry_delay_seconds", 15)
            if not isinstance(retry_attempts, int) or retry_attempts < 0:
                raise ValueError("stage retry_attempts must be a non-negative integer")
            if not isinstance(retry_delay_seconds, int) or retry_delay_seconds < 0:
                raise ValueError("stage retry_delay_seconds must be a non-negative integer")
            allow_nonzero = raw_stage.get("allow_nonzero", False)
            if not isinstance(allow_nonzero, bool):
                raise ValueError("stage allow_nonzero must be a boolean")
            for attempt in range(1, retry_attempts + 2):
                log_suffix = "" if attempt == 1 else f"-attempt-{attempt}"
                log_path = output_dir / f"candidate-{candidate:03d}-{name}{log_suffix}.log"
                stage_started_at = time.time()
                stage_started_monotonic = time.monotonic()
                _write_json(state_path, {"phase": "stage_running", "candidate": candidate, "release_name": release_name, "stage": name, "attempt": attempt, "log_path": str(log_path), "stage_started_at": stage_started_at})
                returncode = command_runner(command, workdir, log_path)
                stage_elapsed_seconds = int(max(0, time.monotonic() - stage_started_monotonic))
                if returncode == 0 or allow_nonzero:
                    break
                if attempt <= retry_attempts:
                    _write_json(state_path, {"phase": "stage_retry_wait", "candidate": candidate, "release_name": release_name, "stage": name, "attempt": attempt, "returncode": returncode, "log_path": str(log_path), "retry_after_seconds": retry_delay_seconds, "retry_started_at": time.time()})
                    time.sleep(retry_delay_seconds)
                    continue
                result = {"perfect": False, "reason": "stage_failed", "candidate": candidate, "stage": name, "returncode": returncode, "log_path": str(log_path), "attempt": attempt}
                _write_json(state_path, {"phase": "failed", **result})
                return result
            required_paths = raw_stage.get("required_paths", [])
            if not isinstance(required_paths, list) or not all(isinstance(item, str) for item in required_paths):
                raise ValueError("stage required_paths must be a list of paths")
            if returncode == 0:
                for raw_path in required_paths:
                    required_path = Path(_render(raw_path, context))
                    if not required_path.is_absolute():
                        required_path = workdir / required_path
                    if not required_path.exists():
                        result = {
                            "perfect": False,
                            "reason": "stage_output_missing",
                            "candidate": candidate,
                            "stage": name,
                            "missing_path": str(required_path),
                        }
                        _write_json(state_path, {"phase": "failed", **result})
                        return result
            if returncode == 0 and completion_value:
                marker_path = Path(_render(completion_value, context))
                if not marker_path.is_absolute():
                    marker_path = workdir / marker_path
                _write_json(marker_path, {
                    "release_name": release_name,
                    "candidate": candidate,
                    "stage": name,
                    "status": "completed",
                })
            outcome = "completed" if returncode == 0 else f"completed with allowed exit {returncode}"
            completed_at = time.time()
            completed = {"event": "stage_completed", "candidate": candidate, "release_name": release_name, "stage": name, "returncode": returncode, "stage_started_at": stage_started_at, "stage_elapsed_seconds": stage_elapsed_seconds, "completed_at": completed_at, "log_path": str(log_path)}
            _write_json(state_path, {"phase": "stage_completed", **completed})
            _append_event(events_path, completed)

        _write_json(state_path, {"phase": "scoring", "candidate": candidate, "release_name": release_name})
        try:
            perfect, scores = _read_scores(reports, context, workdir)
        except ValueError as error:
            result = {"perfect": False, "reason": "score_report_invalid", "candidate": candidate, "error": str(error)}
            _write_json(state_path, {"phase": "failed", **result})
            return result
        if perfect:
            result = {"perfect": True, "candidate": candidate, "release_name": release_name, "scores": scores}
            _write_json(state_path, {"phase": "perfect_score", **result})
            notifier("Training automation complete", f"Candidate {candidate} has perfect configured scores.")
            return result

        _write_json(state_path, {"phase": "score_below_target", "candidate": candidate, "release_name": release_name, "scores": scores})
        removed_artifacts = cleanup_rejected_artifacts(config)
        if removed_artifacts:
            _append_event(events_path, {
                "event": "rejected_artifacts_deleted",
                "candidate": candidate,
                "paths": [str(path) for path in removed_artifacts],
            })
        if candidate == first_candidate + maximum - 1:
            if cap_recovery is not None:
                max_handoffs, recovery_template = cap_recovery
                recovery_state_path = output_dir / "cap_recovery_state.json"
                try:
                    recovery_state = json.loads(recovery_state_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    recovery_state = {}
                except (OSError, json.JSONDecodeError) as error:
                    raise ValueError(f"invalid cap recovery state {recovery_state_path}: {error}") from error
                handoffs = recovery_state.get("handoffs", 0)
                if not isinstance(handoffs, int) or handoffs < 0:
                    raise ValueError("cap recovery state handoffs must be a non-negative integer")
                if handoffs < max_handoffs:
                    scores_path = output_dir / "cap_recovery_scores.json"
                    _write_json(scores_path, {
                        "candidate": candidate,
                        "release_name": release_name,
                        "scores": scores,
                    })
                    recovery_context = {
                        **context,
                        "run_dir": str(output_dir),
                        "scores_path": str(scores_path),
                    }
                    command = [_render(str(part), recovery_context) for part in recovery_template]
                    recovery_log = output_dir / f"candidate-{candidate:03d}-cap-recovery-{handoffs + 1}.log"
                    _write_json(state_path, {
                        "phase": "cap_recovery_running",
                        "candidate": candidate,
                        "release_name": release_name,
                        "reason": "candidate_cap_reached",
                        "scores": scores,
                        "handoff": handoffs + 1,
                        "log_path": str(recovery_log),
                    })
                    returncode = command_runner(command, workdir, recovery_log)
                    if returncode == 0:
                        _write_json(recovery_state_path, {"handoffs": handoffs + 1})
                        result = {
                            "perfect": False,
                            "reason": "cap_recovery_started",
                            "candidate": candidate,
                            "release_name": release_name,
                            "scores": scores,
                            "handoff": handoffs + 1,
                            "log_path": str(recovery_log),
                        }
                        _write_json(state_path, {"phase": "cap_recovery_started", **result})
                        return result
                    result = {
                        "perfect": False,
                        "reason": "cap_recovery_failed",
                        "candidate": candidate,
                        "release_name": release_name,
                        "returncode": returncode,
                        "log_path": str(recovery_log),
                    }
                    _write_json(state_path, {"phase": "failed", **result})
                    return result
            result = {"perfect": False, "reason": "candidate_cap_reached", "candidate": candidate, "scores": scores}
            _write_json(state_path, {"phase": "candidate_cap_reached", **result})
            return result
        for index, raw_command in enumerate(repair_commands, start=1):
            if not isinstance(raw_command, list):
                raise ValueError("each repair command must be a command array")
            command = [_render(str(part), context) for part in raw_command]
            log_path = output_dir / f"candidate-{candidate:03d}-repair-{index}.log"
            _write_json(state_path, {"phase": "repair_running", "candidate": candidate, "stage": f"repair-{index}", "log_path": str(log_path)})
            returncode = command_runner(command, workdir, log_path)
            if returncode != 0:
                result = {"perfect": False, "reason": "repair_failed", "candidate": candidate, "stage": index, "returncode": returncode}
                _write_json(state_path, {"phase": "failed", **result})
                return result
        previous_release_name = release_name

    raise RuntimeError("unreachable")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("config root must be an object")
        result = run_loop(config, run_dir=args.run_dir)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        result = {"perfect": False, "reason": "configuration_error", "error": str(error)}
        _write_json(args.run_dir / "monitor_state.json", {"phase": "failed", **result})
        print(json.dumps(result), flush=True)
        raise SystemExit(2) from error
    print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result["perfect"] else 2)


if __name__ == "__main__":
    main()
