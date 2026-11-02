#!/usr/bin/python3 -I
"""Disk accounting and cancellable backups with journal recovery."""
import json,shutil,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,'/usr/lib/frl')
from server_common import atomic,guard

STOP=False
SHUTDOWN=Path('/run/frl/control/shutdown-in-progress')


def stop(*args):
    global STOP
    STOP=True


def backup_command(action,deadline=300):
    child=subprocess.Popen(['/usr/local/sbin/frl-backup',action],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    end=time.monotonic()+deadline
    try:
        while True:
            if STOP or SHUTDOWN.exists() or time.monotonic()>end:
                child.terminate()
                try:out,err=child.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill();out,err=child.communicate(timeout=5)
                return {'returncode':child.returncode,'cancelled':True,'message':(out+err)[-500:]}
            try:
                out,err=child.communicate(timeout=.5)
                return {'returncode':child.returncode,'message':(out+err)[-500:]}
            except subprocess.TimeoutExpired:pass
    finally:
        if child.poll() is None:
            child.terminate()
            try:child.communicate(timeout=20)
            except subprocess.TimeoutExpired:child.kill();child.communicate(timeout=5)


def main():
    guard();previous=None
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    while not STOP and not SHUTDOWN.exists():
        disk=shutil.disk_usage('/')
        backups=list(Path('/var/backups/frl/server-data').glob('frl-server-*.tar.gz'))
        last=max((p.stat().st_mtime for p in backups),default=0)
        status={'monotonic':time.monotonic(),'free_bytes':disk.free,'free_percent':disk.free/disk.total*100,
                'last_backup':last,'clock_plausible':time.time()>1735689600}
        status['disk_low']=disk.free<256*1024*1024 or status['free_percent']<10
        try:
            status['recovery']=backup_command('recover',60)
            if status['recovery']['returncode']==0 and not STOP and not SHUTDOWN.exists() and not status['disk_low'] and status['clock_plausible'] and time.time()-last>86400:
                status['backup_result']=backup_command('create')
                if not STOP and not SHUTDOWN.exists():status['recovery_after_backup']=backup_command('recover',60)
        except Exception as exc:status['backup_error']=str(exc)
        atomic(Path('/run/frl/server-maintenance.json'),json.dumps(status).encode(),0o644)
        current=(status['disk_low'],status.get('backup_result'),status.get('backup_error'),status.get('recovery',{}).get('returncode'))
        if current!=previous:print(json.dumps(status),flush=True);previous=current
        for _ in range(120):
            if STOP or SHUTDOWN.exists():break
            time.sleep(.5)


if __name__=='__main__':main()
