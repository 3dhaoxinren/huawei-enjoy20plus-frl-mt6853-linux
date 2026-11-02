#!/usr/bin/python3
"""MT6853 desktop-session scheduling; background services keep the root policy."""
import argparse
import contextlib
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

CG = Path('/run/frl/cpu-scheduler')
GROUP = CG/'desktop'
CONFIG = Path('/etc/frl/performance-profile')
PROFILES = {'balanced': (384, 657), 'server': (0, 0)}
CPU = Path('/sys/devices/system/cpu/cpufreq')

def read_profile():
    if not CONFIG.exists():
        return 'balanced'
    meta = CONFIG.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
        raise RuntimeError('profile must be a root-owned non-writable regular file')
    name = CONFIG.read_text().strip()
    if name not in PROFILES:
        raise ValueError('unknown saved profile')
    return name

@contextlib.contextmanager
def locked():
    if os.geteuid() != 0:
        raise PermissionError('native root required')
    if Path('/proc/1/comm').read_text().strip() != 'frl-init':
        raise RuntimeError('native frl-init required')
    if Path('/sys/firmware/devicetree/base/compatible').read_bytes() != b'mediatek,MT6853\0':
        raise RuntimeError('unsupported board')
    fd = os.open('/run/frl/performance.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)

def setup():
    rows = [line.split() for line in Path('/proc/mounts').read_text().splitlines()]
    mounts = [r for r in rows if r[2] == 'cgroup' and 'schedtune' in r[3].split(',')]
    if mounts and (len(mounts) != 1 or mounts[0][1] != str(CG)):
        raise RuntimeError('schedtune is managed elsewhere')
    if not mounts:
        if CG.exists():
            raise RuntimeError('unexpected unmounted scheduler directory')
        CG.mkdir(mode=0o755)
        try:
            subprocess.run(['/usr/bin/mount','-t','cgroup','-o','schedtune,nosuid,nodev,noexec',
                            'schedtune',str(CG)],check=True,timeout=5)
        except BaseException:
            CG.rmdir()
            raise
    if (CG/'schedtune.util.min').read_text().strip() != '0':
        raise RuntimeError('unexpected root utilization clamp')
    if (CG/'schedtune.boost').read_text().strip() != '0':
        raise RuntimeError('unexpected root boost')
    for policy in ('policy0','policy6'):
        if (CPU/policy/'scaling_governor').read_text().strip() != 'schedutil':
            raise RuntimeError('expected schedutil governor')
    GROUP.mkdir(mode=0o755,exist_ok=True)
    for item in ('schedtune.boost','schedtune.prefer_idle'):
        if (GROUP/item).read_text().strip() != '0':
            raise RuntimeError('unexpected desktop policy: '+item)

def set_clamp(requested, expected):
    path = GROUP/'schedtune.util.min'
    path.write_text(str(requested))
    actual = int(path.read_text())
    effective = int((GROUP/'schedtune.util.min.effective').read_text())
    if actual != expected or effective != expected:
        raise RuntimeError(f'kernel clamp differs: requested={requested}, actual={actual}, effective={effective}')

def apply_profile(name):
    setup()
    old = int((GROUP/'schedtune.util.min').read_text())
    try:
        set_clamp(*PROFILES[name])
    except BaseException:
        set_clamp(old,old)
        raise

def save_profile(name):
    fd, temporary = tempfile.mkstemp(prefix='.performance-profile-',dir=str(CONFIG.parent))
    try:
        with os.fdopen(fd,'w') as f:
            os.fchmod(f.fileno(),0o644)
            f.write(name+'\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temporary,CONFIG)
        fd = os.open(CONFIG.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def change_profile(name):
    read_profile()
    apply_profile(name)
    try:
        save_profile(name)
    except BaseException:
        # A directory fsync may fail after rename committed the new profile.
        apply_profile(read_profile())
        raise

def desktop_processes():
    processes = {}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal(): continue
        try:
            if proc.stat().st_uid != 1000: continue
            fields = (proc/'stat').read_text().rsplit(')',1)[1].split()
            processes[int(proc.name)] = (int(fields[1]),fields[19],os.readlink(proc/'exe'))
        except (FileNotFoundError,ProcessLookupError):
            continue
    return processes

def attach_desktop():
    attached = set()
    # Migrate roots before scanning descendants. Later forks then inherit membership.
    for attempt in range(5):
        processes = desktop_processes()
        roots = {pid for pid,(_,_,exe) in processes.items() if exe in ('/usr/bin/weston','/usr/bin/phoc')}
        selected = set(roots)
        while True:
            children = {pid for pid,(parent,_,_) in processes.items() if parent in selected}
            if children <= selected: break
            selected |= children
        pending = []
        for pid in sorted(selected,key=lambda pid: (pid not in roots,pid)):
            try:
                proc = Path('/proc')/str(pid)
                fields = (proc/'stat').read_text().rsplit(')',1)[1].split()
                if proc.stat().st_uid != 1000 or fields[19] != processes[pid][1]: continue
                if any(line.endswith(':schedtune:/desktop') for line in (proc/'cgroup').read_text().splitlines()):
                    continue
                (GROUP/'cgroup.procs').write_text(str(pid))
                attached.add(pid); pending.append(pid)
            except (FileNotFoundError,ProcessLookupError):
                continue
        if attempt > 0 and not pending:
            return sorted(attached)
    raise RuntimeError('desktop process tree did not settle; retry attach-desktop')

def status():
    return {'profile':read_profile(),'group_exists':GROUP.exists(),
            'desktop_util_min':int((GROUP/'schedtune.util.min').read_text()) if GROUP.exists() else None,
            'desktop_pids':(GROUP/'cgroup.procs').read_text().split() if GROUP.exists() else [],
            'policies':[{key:(CPU/n/key).read_text().strip() for key in
                         ('affected_cpus','scaling_governor','scaling_min_freq','scaling_max_freq','scaling_cur_freq')}
                        for n in ('policy0','policy6')],
            'server_profile_stops_desktop':False}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    for name in ('status','apply','attach-desktop'): sub.add_parser(name)
    sub.add_parser('set').add_argument('profile',choices=PROFILES)
    sub.add_parser('exec-desktop').add_argument('argv',nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == 'status':
        print(json.dumps(status())); return
    if args.command == 'exec-desktop':
        argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
        if not argv or argv[0] not in ('/usr/bin/weston','/usr/bin/phoc'):
            parser.error('exec-desktop requires /usr/bin/weston or /usr/bin/phoc')
        if os.geteuid() != 0: raise PermissionError('native root required')
        try:
            with locked():
                apply_profile(read_profile())
                (GROUP/'cgroup.procs').write_text(str(os.getpid()))
        except Exception as exc:
            print('CPU policy unavailable; starting desktop with existing scheduling: '+str(exc),file=sys.stderr)
        os.execv('/usr/bin/chpst',['/usr/bin/chpst','-u','frl:frl',*argv])
    with locked():
        if args.command == 'set': change_profile(args.profile)
        else:
            apply_profile(read_profile())
            if args.command == 'attach-desktop':
                print(json.dumps({'attached':attach_desktop()}))
        print(json.dumps(status()))

if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print('frl-performance: '+str(exc),file=sys.stderr)
        sys.exit(1)
