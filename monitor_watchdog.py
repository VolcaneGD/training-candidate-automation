"""Hidden watchdog that restarts a stalled Training Monitor window."""
from __future__ import annotations
import argparse, json, subprocess, time
from pathlib import Path
from training_monitor import SingleInstanceMutex, monitor_heartbeat_stale, monitor_heartbeat_path

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument('--instance-key',required=True); p.add_argument('--launcher',type=Path,required=True); p.add_argument('--title',required=True); p.add_argument('--watch-path',required=True); p.add_argument('--log-path',required=True); p.add_argument('--state-path',type=Path,required=True); p.add_argument('--process-id',type=int,required=True); a=p.parse_args()
    mutex=SingleInstanceMutex(a.instance_key+'-watchdog')
    if not mutex.acquire(): return 0
    heartbeat=monitor_heartbeat_path(a.instance_key)
    while True:
        try: state=json.loads(a.state_path.read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError): state={}
        if state.get('phase')=='perfect_score': return 0
        try: rendered=json.loads(heartbeat.read_text(encoding='utf-8')).get('rendered_at')
        except (OSError,json.JSONDecodeError): rendered=None
        if monitor_heartbeat_stale(rendered):
            subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(a.launcher),'-Title',a.title,'-WatchPath',a.watch_path,'-LogPath',a.log_path,'-StatePath',str(a.state_path),'-ProcessId',str(a.process_id),'-InstanceKey',a.instance_key,'-ReplaceExisting','-NoNotify'],check=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        time.sleep(2)
if __name__=='__main__': raise SystemExit(main())
