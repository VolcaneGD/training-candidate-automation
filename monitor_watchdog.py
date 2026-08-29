"""Hidden watchdog that restarts a stalled Training Monitor window."""
from __future__ import annotations
import argparse, json, subprocess, time
from pathlib import Path
from training_monitor import (
    SingleInstanceMutex,
    monitor_heartbeat_stale,
    monitor_heartbeat_path,
    monitor_render_progress_stalled,
)

def recovery_launcher_command(arguments: argparse.Namespace) -> list[str]:
    """Restart only the monitor window, never this watchdog."""
    return ['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(arguments.launcher),'-Title',arguments.title,'-WatchPath',arguments.watch_path,'-LogPath',arguments.log_path,'-StatePath',str(arguments.state_path),'-ProcessId',str(arguments.process_id),'-InstanceKey',arguments.instance_key,'-ReplaceExisting','-KeepWatchdog','-NoNotify']

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument('--instance-key',required=True); p.add_argument('--launcher',type=Path,required=True); p.add_argument('--title',required=True); p.add_argument('--watch-path',required=True); p.add_argument('--log-path',required=True); p.add_argument('--state-path',type=Path,required=True); p.add_argument('--process-id',type=int,required=True); a=p.parse_args()
    mutex=SingleInstanceMutex(a.instance_key+'-watchdog')
    if not mutex.acquire(): return 0
    heartbeat=monitor_heartbeat_path(a.instance_key)
    previous_payload: object = None
    while True:
        try: state=json.loads(a.state_path.read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError): state={}
        if state.get('phase')=='perfect_score': return 0
        try: payload=json.loads(heartbeat.read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError): payload={}
        rendered = payload.get('rendered_at') if isinstance(payload, dict) else None
        if monitor_heartbeat_stale(rendered) or monitor_render_progress_stalled(previous_payload, payload):
            subprocess.run(recovery_launcher_command(a),check=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            previous_payload = None
        else:
            previous_payload = payload
        time.sleep(2)
if __name__=='__main__': raise SystemExit(main())
