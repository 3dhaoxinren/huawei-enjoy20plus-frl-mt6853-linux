#!/usr/bin/python3
import argparse,fcntl,json,os,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,'/usr/lib/frl')
from server_common import MODE,GRAPHICAL,atomic,guard,runtime_mode,ssh_listening

LIGHT=Path('/sys/class/leds/lcd-backlight/brightness')
SERVICE=Path('/run/frl/service')
SHUTDOWN=Path('/run/frl/control/shutdown-in-progress')

def switching_allowed():
    if SHUTDOWN.exists():raise RuntimeError('shutdown is in progress')

def command(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=20)

def flags(server):
    for name in GRAPHICAL:
        path=SERVICE/name/'down'
        if server:atomic(path,b'',0o644)
        else:path.unlink(missing_ok=True)

def apply(server):
    switching_allowed()
    flags(server)
    if server:
        for name in GRAPHICAL:
            switching_allowed();command('/usr/bin/sv','-w','12','down',str(SERVICE/name))
        command('/usr/local/sbin/frl-performance','set','server')
        LIGHT.write_text('0\n')
    else:
        command('/usr/local/sbin/frl-performance','set','balanced')
        for name in ('seatd','power','desktop'):
            switching_allowed();command('/usr/bin/sv','up',str(SERVICE/name))

def validate_ready(mode,timeout=30):
    sys.path.insert(0,'/usr/lib/frl')
    import importlib.util
    spec=importlib.util.spec_from_file_location('frl_health','/usr/lib/frl/health-feed.py')
    health=importlib.util.module_from_spec(spec);spec.loader.exec_module(health)
    end=time.monotonic()+timeout
    while True:
        switching_allowed()
        try:
            result=health.check()
            if result['healthy'] and result['runtime_mode']==mode:return result
        except Exception:
            pass
        if time.monotonic()>=end:raise RuntimeError('mode readiness deadline expired')
        time.sleep(.5)

def wait_desktop():
    import importlib.util
    spec=importlib.util.spec_from_file_location('frl_wayland','/usr/lib/frl/health-feed.py')
    health=importlib.util.module_from_spec(spec);spec.loader.exec_module(health)
    deadline=time.monotonic()+25
    while True:
        switching_allowed()
        try:health.wayland_sync('/run/user/1000/wayland-0');return
        except Exception:
            if time.monotonic()>=deadline:raise RuntimeError('desktop did not become ready')
            time.sleep(.5)

def rollback(previous):
    errors=[]
    if SHUTDOWN.exists():return ['shutdown has priority; service restart skipped']
    if previous=='server':
        try:atomic(MODE,b'server\n')
        except Exception as exc:errors.append('restore mode: '+str(exc))
        # Reconcile services with the visible token even if its fsync failed.
        actual=runtime_mode()
        try:
            apply(actual=='server')
            if actual=='desktop':wait_desktop()
        except Exception as exc:errors.append('restore services: '+str(exc))
    else:
        try:apply(False);wait_desktop()
        except Exception as exc:errors.append('restore desktop: '+str(exc));return errors
        try:atomic(MODE,b'desktop\n')
        except Exception as exc:errors.append('restore mode: '+str(exc))
    return errors

def change(mode):
    switching_allowed()
    fd=os.open('/run/frl/server-mode.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        switching_allowed()
        previous=runtime_mode()
        try:
            if mode=='server':
                if not ssh_listening():raise RuntimeError('SSH must listen before switching to server mode')
                atomic(MODE,b'server\n');apply(True)
            else:
                # Keep headless health valid while the graphical session starts.
                apply(False)
                wait_desktop()
                atomic(MODE,b'desktop\n')
            result=validate_ready(mode)
            print(json.dumps({'mode':mode,'health':result}))
        except BaseException as exc:
            errors=rollback(previous)
            record={'requested':mode,'previous':previous,'error':str(exc),'rollback_errors':errors,'monotonic':time.monotonic()}
            try:atomic(Path('/run/frl/server-mode-transition.json'),json.dumps(record).encode(),0o600)
            except Exception:pass
            raise RuntimeError('mode change failed: '+str(exc)+'; rollback: '+json.dumps(errors)) from exc
    finally:os.close(fd)

def boot():
    server=runtime_mode()=='server'
    flags(server)
    if server:
        command('/usr/local/sbin/frl-performance','set','server')
        LIGHT.write_text('0\n')

def status():
    services={}
    for name in ('ssh','desktop','seatd','power','radio','wpa','dhcp','fuel','charge-limit','chrony','apps'):
        path=SERVICE/name
        if path.exists():services[name]=subprocess.run(['/usr/bin/sv','status',str(path)],capture_output=True,text=True,timeout=3).stdout.strip()
    return {'mode':runtime_mode(),'ssh_listening':ssh_listening(),'services':services,
            'health':json.loads(Path('/run/frl/control/health.json').read_text()),
            'ipv4':json.loads(subprocess.check_output(['/usr/sbin/ip','-j','-4','addr','show','dev','wlan0'],text=True)),
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}

def main():
    parser=argparse.ArgumentParser(description='Switch the native FRL desktop/server session')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('status');sub.add_parser('boot')
    sub.add_parser('mode').add_argument('mode',choices=('server','desktop'))
    args=parser.parse_args();guard()
    if args.command=='status':print(json.dumps(status()))
    elif args.command=='boot':boot()
    else:change(args.mode)

if __name__=='__main__':
    try:main()
    except Exception as exc:print('frl-server: '+str(exc),file=sys.stderr);sys.exit(1)
