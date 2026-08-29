"""Standalone Windows monitor for model-training jobs and artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable


RUNTIME_SETTING_NAMES = (
    "title", "watch_path", "log_path", "process_id", "modal_app",
    "state_path", "refresh_seconds", "notify", "recovery_task",
)
RECOVERY_CONFIRMATION_SECONDS = 30
HEARTBEAT_STALE_SECONDS = 45

TEXT = {
    "en": {"live_log": "LIVE LOG", "language": "日本語", "candidate": "CANDIDATE", "stage": "STAGE", "elapsed": "ELAPSED", "artifacts": "ARTIFACTS", "action": "ACTION REQUIRED — Candidate safety cap reached. Report this screen to Codex to inspect the score, improve the curriculum, set a new positive cap, and resume a bounded run."},
    "ja": {"live_log": "ライブログ", "language": "English", "candidate": "現候補", "stage": "ステージ", "elapsed": "経過時間", "artifacts": "アーティファクト", "action": "要対応 — 学習候補の安全上限に到達しました。この画面を Codex に報告し、スコア確認、カリキュラム改善、新しい正の上限設定、有界実行の再開を依頼してください。"},
}


def dashboard_text(language: str, key: str) -> str:
    return TEXT.get(language, TEXT["en"]).get(key, TEXT["en"].get(key, key))


def dashboard_stage_label(stage: object, language: str) -> str:
    value = str(stage).replace("_", " ")
    if language != "ja":
        return value
    return {
        "upload curriculum": "カリキュラムをアップロード",
        "modal train": "Modal で学習",
        "modal merge": "Modal でマージ",
        "download merged": "マージ済みモデルをダウンロード",
        "convert q8 gguf": "Q8 GGUF に変換",
        "fixed eval": "固定評価",
        "boundary eval": "境界評価",
        "multitool eval": "マルチツール評価",
        "runtime eval": "実行時評価",
    }.get(value, value)


def safety_cap_guidance(language: str) -> str:
    return dashboard_text(language, "action")


def stage_failure_guidance(language: str) -> str:
    return (
        "ACTION REQUIRED — Stage retries are exhausted. Report this screen to Codex so "
        "the failed stage and its log can be inspected before resuming."
        if language != "ja" else
        "要対応 — 工程の再試行回数を使い切りました。この画面を Codex に報告し、失敗した工程とログを確認してから再開を依頼してください。"
    )


def dashboard_font(language: str) -> str:
    """Prefer the installed UD font whenever the dashboard is in Japanese."""
    return "BIZ UDPGothic" if language == "ja" else "Segoe UI"


def dashboard_theme() -> dict[str, str]:
    return {
        "background": "#101318",
        "surface": "#181d25",
        "surface_muted": "#222936",
        "border": "#2d3748",
        "text": "#edf2f7",
        "muted": "#94a3b8",
        "running": "#5eead4",
        "completed": "#93c5fd",
        "failed": "#fb7185",
        "unknown": "#fbbf24",
    }


def dashboard_phase_label(phase: object) -> str:
    labels = {
        "candidate_started": "Preparing candidate",
        "stage_running": "Running stage",
        "stage_retry_wait": "Error shown; retrying stage",
        "stage_completed": "Stage completed",
        "stage_skipped": "Reusing artifact",
        "scoring": "Checking scores",
        "score_below_target": "Scheduling retry",
        "repair_running": "Refreshing curriculum",
        "candidate_cap_reached": "Candidate cap reached",
        "perfect_score": "All checks passed",
        "failed": "Action required",
    }
    return labels.get(str(phase), str(phase).replace("_", " ").title() or "Unrecognized")


def dashboard_status_badge(phase: object, overall_state: object) -> tuple[str, str, bool]:
    """Return the Status Badge text, accent color, and pulse state."""
    value = str(phase)
    if value == "perfect_score":
        return "COMPLETE", "#86efac", False
    if value in {"failed", "candidate_cap_reached"} or str(overall_state) == "failed":
        return "STOPPED", "#fb7185", False
    if str(overall_state) != "running":
        return "STOPPED", "#fb7185", False
    if value in {"stage_retry_wait", "orchestrator_recovering"}:
        return "RETRYING", "#fbbf24", False
    if value in {"stage_running", "repair_running", "candidate_started", "scoring"}:
        return "RUNNING", "#5eead4", True
    return "STOPPED", "#94a3b8", False


def recovery_request_key(automation_state: object) -> str | None:
    """Return a stable key when automation has stopped and needs its task restarted."""
    if not isinstance(automation_state, dict):
        return None
    phase = str(automation_state.get("phase") or "")
    reason = str(automation_state.get("reason") or "")
    if phase == "candidate_cap_reached" or reason in {
        "candidate_cap_reached", "cap_recovery_failed", "stage_failed",
    }:
        return None
    if phase not in {"failed", "candidate_cap_reached"}:
        return None
    candidate = automation_state.get("candidate")
    return f"{phase}:{candidate}" if candidate is not None else phase


def recovery_status_badge(
    phase: object,
    overall_state: object,
    *,
    recovery_task: str | None,
    request_started_at: float | None,
    request_accepted: bool,
    now: float | None = None,
) -> tuple[str, str, bool]:
    """Show RETRYING only while a configured recovery launch awaits confirmation."""
    fallback = dashboard_status_badge(phase, overall_state)
    if recovery_request_key({"phase": phase}) is None or not recovery_task:
        return fallback
    if request_started_at is None:
        return fallback
    return "RETRYING", "#fbbf24", False


def recovery_request_due(last_request_at: float | None, *, now: float | None = None) -> bool:
    """Return whether automatic recovery must be requested again."""
    if last_request_at is None:
        return True
    current_time = time.monotonic() if now is None else now
    return current_time - last_request_at >= RECOVERY_CONFIRMATION_SECONDS


def request_orchestrator_recovery(task_name: str | None) -> bool:
    """Ask Task Scheduler to run the recovery handler without showing a console."""
    if not task_name:
        return False
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["schtasks", "/Run", "/TN", task_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=creationflags,
        )
        return result.returncode == 0
    except OSError:
        return False


def should_close_after_terminal_state(automation_state: object) -> bool:
    """Keep a successful completion visible briefly, then end this monitor."""
    return isinstance(automation_state, dict) and automation_state.get("phase") == "perfect_score"


def completion_log_summary(state: dict[str, object], language: str = "en") -> str:
    """Render terminal automation state at the top of the LIVE LOG."""
    phase = str(state.get("phase") or "")
    candidate = state.get("candidate", "-")
    if phase == "perfect_score":
        lines = [f"COMPLETE — Candidate {candidate}"]
        scores = state.get("scores", [])
        if isinstance(scores, list):
            for score in scores:
                if not isinstance(score, dict):
                    continue
                path = Path(str(score.get("path") or ""))
                label = path.stem.rsplit(".", 1)[-1] or "score"
                passed, cases = score.get("passed"), score.get("cases")
                if isinstance(passed, int) and isinstance(cases, int):
                    lines.append(f"{label}: {passed}/{cases}")
        cleanup = state.get("cleanup")
        if isinstance(cleanup, dict) and cleanup.get("candidates"):
            preview_command = cleanup.get("preview_command")
            if isinstance(preview_command, str) and preview_command:
                lines.extend(["", "ARTIFACT CLEANUP RECOMMENDED", preview_command])
        return "\n".join(lines)
    if phase in {"failed", "candidate_cap_reached"}:
        reason = str(state.get("reason") or phase)
        stage = str(state.get("stage") or "-")
        summary = f"STOPPED — Candidate {candidate}\nreason: {reason}\nstage: {stage}"
        if reason in {"candidate_cap_reached", "cap_recovery_failed"}:
            summary += "\n\n" + safety_cap_guidance(language)
        elif reason == "stage_failed":
            summary += "\n\n" + stage_failure_guidance(language)
        return summary
    return ""


def completion_summary_color(state: object) -> str | None:
    """Return the Status Badge color for a terminal LIVE LOG summary."""
    if not isinstance(state, dict):
        return None
    phase = str(state.get("phase") or "")
    if phase == "perfect_score":
        return "#86efac"
    if phase in {"failed", "candidate_cap_reached"}:
        return "#fb7185"
    return None


def dashboard_activity_label(elapsed_seconds: int, refresh_tick: int, *, is_running: bool = True) -> str:
    """Provide a visible heartbeat while a long stage is making no new log line."""
    if not is_running:
        return f"Stopped {elapsed_seconds // 60:02d}:{elapsed_seconds % 60:02d}"
    indicators = ("·", "··", "···", "····")
    return f"Monitoring {elapsed_seconds // 60:02d}:{elapsed_seconds % 60:02d}  {indicators[refresh_tick % len(indicators)]}"


def stage_elapsed_seconds(state: dict[str, object], *, now: float | None = None) -> int:
    """Return the current stage duration, frozen whenever no stage is running."""
    phase = state.get("phase")
    if phase in {"stage_running", "repair_running", "stage_retry_wait"}:
        started_at = state.get("retry_started_at") if phase == "stage_retry_wait" else state.get("stage_started_at")
        if isinstance(started_at, (int, float)):
            return int(max(0, (time.time() if now is None else now) - started_at))
    elapsed = state.get("stage_elapsed_seconds", 0)
    return elapsed if isinstance(elapsed, int) and elapsed >= 0 else 0


def dashboard_stage_elapsed_value(snapshot: dict[str, object]) -> str:
    elapsed = snapshot.get("stage_elapsed_seconds", 0)
    seconds = elapsed if isinstance(elapsed, int) and elapsed >= 0 else 0
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def dashboard_state_color(state: object) -> str:
    theme = dashboard_theme()
    return theme.get(str(state), theme["unknown"])


def singleton_mutex_name(instance_key: str) -> str:
    """Return a Windows-safe stable name for one monitor instance."""
    digest = hashlib.sha256(instance_key.encode("utf-8")).hexdigest()[:24]
    return f"Local\\TrainingCandidateMonitor-{digest}"


def runtime_settings_path(instance_key: str) -> Path:
    digest = hashlib.sha256(instance_key.encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"training-candidate-monitor-{digest}.json"


def apply_runtime_settings(args: argparse.Namespace, payload: dict[str, object]) -> argparse.Namespace:
    """Copy the allowed runtime fields from a launcher update into monitor args."""
    updated = argparse.Namespace(**vars(args))
    for name in ("watch_path", "log_path", "state_path"):
        value = payload.get(name)
        if isinstance(value, str) and value:
            setattr(updated, name, Path(value))
        elif value is None:
            setattr(updated, name, None)
    for name in ("title", "modal_app", "recovery_task"):
        value = payload.get(name)
        if isinstance(value, str):
            setattr(updated, name, value or None if name == "modal_app" else value)
    value = payload.get("process_id")
    if isinstance(value, int) and value > 0:
        updated.process_id = value
    elif value is None:
        updated.process_id = None
    value = payload.get("refresh_seconds")
    if isinstance(value, int) and value > 0:
        updated.refresh_seconds = value
    value = payload.get("notify")
    if isinstance(value, bool):
        updated.notify = value
    return updated


def write_runtime_settings(path: Path, args: argparse.Namespace) -> None:
    payload: dict[str, object] = {}
    for name in RUNTIME_SETTING_NAMES:
        value = getattr(args, name)
        payload[name] = str(value) if isinstance(value, Path) else value
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class SingleInstanceMutex:
    def __init__(self, instance_key: str) -> None:
        self._handle: object | None = None
        self._acquired = False
        self._name = singleton_mutex_name(instance_key)

    def acquire(self) -> bool:
        if os.name != "nt":
            self._acquired = True
            return True
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            raise OSError("CreateMutexW failed")
        self._handle = handle
        self._acquired = kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
        return self._acquired

    def close(self) -> None:
        if self._handle is not None and os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._handle)
        self._handle = None


def process_exists(process_id: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {process_id}", "/NH"],
            text=True,
            capture_output=True,
            check=False,
        )
        return str(process_id) in result.stdout
    return Path(f"/proc/{process_id}").exists()


def modal_state(app_id: str) -> str:
    try:
        result = subprocess.run(
            ["modal", "app", "list"], text=True, capture_output=True, check=False, timeout=15
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unavailable"
    if result.returncode != 0:
        return "unavailable"
    for line in result.stdout.splitlines():
        if app_id in line:
            match = re.search(r"\b(running|stopping|stopped|ephemeral|failed)\b", line, re.I)
            return match.group(1).lower() if match else "unknown"
    return "not found"


def directory_stats(path: Path | None) -> tuple[int, int]:
    if path is None or not path.exists():
        return 0, 0
    files = [item for item in path.rglob("*") if item.is_file()]
    return len(files), sum(item.stat().st_size for item in files)


def tail_text(path: Path | None, lines: int = 2) -> str:
    if path is None or not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def automation_state(path: Path | None) -> dict[str, object]:
    if path is None or not path.is_file():
        return {}
    try:
        value = __import__("json").loads(path.read_text(encoding="utf-8"))
    except (__import__("json").JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def active_stage_failure_reason(
    state: dict[str, object], process_exists: Callable[[int], bool], *, now: float | None = None,
) -> str | None:
    """Detect a dead command or stale loop heartbeat while an active phase is displayed."""
    if state.get("phase") not in {"stage_running", "repair_running"}:
        return None
    process_id = state.get("command_process_id")
    if isinstance(process_id, int) and process_id > 0 and not process_exists(process_id):
        return "stage_process_exited"
    heartbeat_at = state.get("heartbeat_at")
    current_time = time.time() if now is None else now
    if isinstance(heartbeat_at, (int, float)) and current_time - heartbeat_at > HEARTBEAT_STALE_SECONDS:
        return "stage_heartbeat_stale"
    return None


def build_snapshot(
    *,
    watch_path: Path | None,
    log_path: Path | None,
    process_id: int | None,
    modal_app: str | None,
    state_path: Path | None = None,
    process_exists: Callable[[int], bool] = process_exists,
    modal_state_reader: Callable[[str], str] = modal_state,
) -> dict[str, object]:
    artifact_files, artifact_bytes = directory_stats(watch_path)
    loop_state = automation_state(state_path)
    liveness_reason = active_stage_failure_reason(loop_state, process_exists)
    tracked_process_id = process_id
    loop_process_id = loop_state.get("loop_process_id")
    if isinstance(loop_process_id, int) and loop_process_id > 0 and process_exists(loop_process_id):
        tracked_process_id = loop_process_id
    if liveness_reason:
        local_state = "failed"
    else:
        local_state = "not tracked" if tracked_process_id is None else ("running" if process_exists(tracked_process_id) else "completed")
    active_log_path = log_path
    stage_log_value = loop_state.get("log_path")
    if local_state != "completed" and isinstance(stage_log_value, str) and stage_log_value:
        stage_log_path = Path(stage_log_value)
        if stage_log_path.is_file():
            active_log_path = stage_log_path
    remote_state = "not tracked" if modal_app is None else modal_state_reader(modal_app)
    states = {local_state, remote_state}
    if "failed" in states:
        overall = "failed"
    elif "running" in states or "stopping" in states:
        overall = "running"
    elif states <= {"not tracked"}:
        overall = "idle"
    elif "unavailable" in states or "unknown" in states or "not found" in states:
        overall = "unknown"
    else:
        overall = "completed"
    return {
        "overall_state": overall,
        "local_state": local_state,
        "tracked_process_id": tracked_process_id,
        "modal_state": remote_state,
        "artifact_files": artifact_files,
        "artifact_bytes": artifact_bytes,
        "log_tail": tail_text(active_log_path),
        "automation_phase": loop_state.get("phase", "not tracked"),
        "automation_candidate": loop_state.get("candidate", "-"),
        "automation_stage": loop_state.get("stage", "-"),
        "automation_state": loop_state,
        "liveness_reason": liveness_reason,
        "stage_elapsed_seconds": stage_elapsed_seconds(loop_state),
    }


def format_bytes(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024
    return f"{value:.1f} TiB"


def send_notification(title: str, message: str) -> None:
    if os.name != "nt":
        return
    # The shared completion helper uses a visible WScript popup plus sound.  A
    # popup is deliberately used here because unregistered WinRT toast calls can
    # succeed without ever appearing in the user's notification center.
    helper = Path.home() / ".codex" / "skills" / "notify-user-on-completion" / "scripts" / "notify_done.py"
    if not helper.is_file():
        return
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [sys.executable, str(helper), "--title", title, "--message", message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError:
        # Notification delivery must never interrupt a training or evaluation stage.
        return


class TrainingMonitorApp:
    def __init__(self, args: argparse.Namespace, settings_path: Path) -> None:
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        self.args = args
        self.language = "en"
        self.settings_path = settings_path
        self.settings_mtime_ns = -1
        self.tk = tk.Tk()
        self.tk.title(args.title)
        self.theme = dashboard_theme()
        self.tk.configure(background=self.theme["background"])
        self.tk.minsize(560, 330)
        self.tk.geometry("600x390")
        self.started = time.monotonic()
        self.refresh_tick = 0
        self.badge_pulsing = False
        self.badge_lit = False
        self.badge_color = self.theme["unknown"]
        self.notified: set[str] = set()
        self.recovery_requests: dict[str, tuple[float, bool, int]] = {}
        self.close_scheduled = False
        self.state_value = tk.StringVar(value="CONNECTING")
        self.phase_value = tk.StringVar(value="Loading monitor")
        self.metrics = {
            "candidate": tk.StringVar(value="-"),
            "stage": tk.StringVar(value="-"),
            "elapsed": tk.StringVar(value="00:00"),
            "artifacts": tk.StringVar(value="0 files"),
        }
        self._build_dashboard(tk, ttk, scrolledtext)
        self._animate_badge()
        self.refresh()

    def _build_dashboard(self, tk: object, ttk: object, scrolledtext: object) -> None:
        header = tk.Frame(self.tk, background=self.theme["background"])
        header.pack(fill="x", padx=16, pady=(14, 8))
        title = tk.Label(
            header, text=self.args.title, anchor="w", background=self.theme["background"],
            foreground=self.theme["text"], font=("Segoe UI Semibold", 12),
        )
        title.pack(side="left", fill="x", expand=True)
        self.state_badge = tk.Label(
            header, textvariable=self.state_value, background=self.theme["surface_muted"],
            foreground=self.theme["unknown"], font=("Segoe UI Semibold", 9), padx=9, pady=4,
        )
        self.state_badge.pack(side="right")

        phase = tk.Label(
            self.tk, textvariable=self.phase_value, anchor="w", background=self.theme["background"],
            foreground=self.theme["muted"], font=("Segoe UI", 9),
        )
        phase.pack(fill="x", padx=16, pady=(0, 10))

        cards = tk.Frame(self.tk, background=self.theme["background"])
        cards.pack(fill="x", padx=16)
        self.metric_labels = {}
        for column, (key, value) in enumerate((("candidate", self.metrics["candidate"]), ("stage", self.metrics["stage"]), ("elapsed", self.metrics["elapsed"]), ("artifacts", self.metrics["artifacts"]))):
            cards.grid_columnconfigure(column, weight=1, uniform="metric")
            card = tk.Frame(cards, background=self.theme["surface"], highlightbackground=self.theme["border"], highlightthickness=1)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 4, 0))
            metric_label = tk.Label(card, text=dashboard_text(self.language, key), background=self.theme["surface"], foreground=self.theme["muted"], font=("Segoe UI", 7))
            metric_label.pack(anchor="w", padx=8, pady=(7, 1))
            self.metric_labels[key] = metric_label
            tk.Label(card, textvariable=value, background=self.theme["surface"], foreground=self.theme["text"], font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=8, pady=(0, 7))

        self.log_label = tk.Label(self.tk, text=dashboard_text(self.language, "live_log"), anchor="w", background=self.theme["background"], foreground=self.theme["muted"], font=("Segoe UI", 8))
        self.log_label.pack(fill="x", padx=16, pady=(12, 4))
        self.log = scrolledtext.ScrolledText(
            self.tk, height=9, wrap="word", state="disabled", borderwidth=0,
            background=self.theme["surface"], foreground=self.theme["text"], insertbackground=self.theme["text"],
            selectbackground="#334155", font=("Cascadia Mono", 9), padx=10, pady=9,
        )
        self.log.tag_configure("terminal-complete", foreground="#86efac")
        self.log.tag_configure("terminal-stopped", foreground="#fb7185")
        self.log.tag_configure("terminal-retrying", foreground="#fbbf24")
        self.log.pack(fill="both", expand=True, padx=16)
        actions = tk.Frame(self.tk, background=self.theme["background"])
        actions.pack(fill="x", padx=16, pady=(8, 12))
        style = ttk.Style(self.tk)
        style.configure("Monitor.TButton", font=("Segoe UI Semibold", 9), padding=(10, 3))
        ttk.Button(actions, text="Refresh now", style="Monitor.TButton", command=self.refresh).pack(side="right")
        self.language_button = ttk.Button(actions, text=dashboard_text(self.language, "language"), style="Monitor.TButton", command=self._toggle_language)
        self.language_button.pack(side="left")

    def _toggle_language(self) -> None:
        self.language = "ja" if self.language == "en" else "en"
        self.log_label.configure(text=dashboard_text(self.language, "live_log"))
        self.language_button.configure(text=dashboard_text(self.language, "language"))
        for key, label in self.metric_labels.items():
            label.configure(text=dashboard_text(self.language, key))
        self.log_label.configure(font=(dashboard_font(self.language), 9))
        self.log.configure(font=("BIZ UDPGothic" if self.language == "ja" else "Cascadia Mono", 10 if self.language == "ja" else 9))
        self.refresh()

    def _animate_badge(self) -> None:
        if self.badge_pulsing:
            self.badge_lit = not self.badge_lit
            self.state_badge.configure(
                foreground=self.badge_color,
                background=self.theme["surface"] if self.badge_lit else self.theme["surface_muted"],
            )
        self.tk.after(700, self._animate_badge)

    def _reload_runtime_settings(self) -> None:
        try:
            modified = self.settings_path.stat().st_mtime_ns
            if modified == self.settings_mtime_ns:
                return
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
        except (OSError, json.JSONDecodeError):
            return
        self.settings_mtime_ns = modified
        self.args = apply_runtime_settings(self.args, payload)
        self.tk.title(self.args.title)

    def refresh(self) -> None:
        self._reload_runtime_settings()
        snapshot = build_snapshot(
            watch_path=self.args.watch_path,
            log_path=self.args.log_path,
            process_id=self.args.process_id,
            modal_app=self.args.modal_app,
            state_path=self.args.state_path,
        )
        state = str(snapshot["overall_state"])
        recovery_key = recovery_request_key(snapshot["automation_state"])
        now = time.monotonic()
        previous_recovery = self.recovery_requests.get(recovery_key) if recovery_key else None
        if recovery_key and recovery_request_due(
            None if previous_recovery is None else previous_recovery[0], now=now,
        ):
            previous_attempts = 0 if previous_recovery is None else previous_recovery[2]
            self.recovery_requests[recovery_key] = (
                now,
                request_orchestrator_recovery(self.args.recovery_task),
                previous_attempts + 1,
            )
        elapsed_value = snapshot["stage_elapsed_seconds"]
        elapsed = elapsed_value if isinstance(elapsed_value, int) and elapsed_value >= 0 else 0
        stage_running = snapshot["automation_phase"] in {"stage_running", "repair_running", "stage_retry_wait"}
        request_started_at, request_accepted, recovery_attempts = (
            self.recovery_requests.get(recovery_key, (None, False, 0)) if recovery_key else (None, False, 0)
        )
        badge_text, badge_color, badge_pulsing = recovery_status_badge(
            snapshot["automation_phase"], state,
            recovery_task=self.args.recovery_task,
            request_started_at=request_started_at,
            request_accepted=request_accepted,
        )
        self.state_value.set(badge_text)
        self.badge_color = badge_color
        self.badge_pulsing = badge_pulsing
        if not badge_pulsing:
            self.badge_lit = False
            self.state_badge.configure(foreground=badge_color, background=self.theme["surface_muted"])
        self.phase_value.set(
            f"{dashboard_phase_label(snapshot['automation_phase'])}  •  "
            f"Local {snapshot['local_state']}  •  Modal {snapshot['modal_state']}  •  "
            f"{dashboard_activity_label(elapsed, self.refresh_tick, is_running=stage_running)}"
        )
        self.metrics["candidate"].set(str(snapshot["automation_candidate"]))
        self.metrics["stage"].set(dashboard_stage_label(snapshot["automation_stage"], self.language))
        self.metrics["elapsed"].set(dashboard_stage_elapsed_value(snapshot))
        self.metrics["artifacts"].set(f"{snapshot['artifact_files']} / {format_bytes(int(snapshot['artifact_bytes']))}")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        terminal_summary = completion_log_summary(snapshot["automation_state"], self.language)
        log_text = str(snapshot["log_tail"]) or "No log output yet."
        recovery_summary = ""
        if recovery_key and self.args.recovery_task:
            request_result = "accepted" if request_accepted else "not accepted; retry scheduled"
            recovery_summary = (
                f"AUTO-RECOVERY — attempt {recovery_attempts}\n"
                f"task: {self.args.recovery_task}\n"
                f"request: {request_result}; retry interval: {RECOVERY_CONFIRMATION_SECONDS}s"
            )
        if terminal_summary:
            summary_color = completion_summary_color(snapshot["automation_state"])
            summary_tag = "terminal-complete" if summary_color == "#86efac" else "terminal-stopped"
            self.log.insert("1.0", terminal_summary, summary_tag)
            if recovery_summary:
                self.log.insert("end", f"\n\n{recovery_summary}", "terminal-retrying")
            self.log.insert("end", f"\n\n{log_text}")
        else:
            self.log.insert("1.0", log_text)
        self.log.configure(state="disabled")
        if self.args.notify and state in {"completed", "failed"} and state not in self.notified:
            send_notification(self.args.title, f"Job {state}.")
            self.notified.add(state)
        if should_close_after_terminal_state(snapshot["automation_state"]) and not self.close_scheduled:
            self.close_scheduled = True
            self.tk.after(5000, self.tk.destroy)
        self.refresh_tick += 1
        self.tk.after(self.args.refresh_seconds * 1000, self.refresh)

    def run(self) -> None:
        self.tk.mainloop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="Training Monitor")
    parser.add_argument("--watch-path", type=Path)
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--process-id", type=int)
    parser.add_argument("--modal-app")
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--refresh-seconds", type=int, default=1)
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--recovery-task", default="")
    parser.add_argument("--instance-key", default="training-candidate-monitor")
    parser.add_argument("--settings-path", type=Path)
    args = parser.parse_args()
    if args.refresh_seconds < 1:
        parser.error("--refresh-seconds must be positive")
    return args


def main() -> None:
    args = parse_args()
    settings_path = args.settings_path or runtime_settings_path(args.instance_key)
    write_runtime_settings(settings_path, args)
    mutex = SingleInstanceMutex(args.instance_key)
    if not mutex.acquire():
        return
    try:
        TrainingMonitorApp(args, settings_path).run()
    finally:
        mutex.close()


if __name__ == "__main__":
    main()
