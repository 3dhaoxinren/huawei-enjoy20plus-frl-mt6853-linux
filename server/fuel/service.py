#!/usr/bin/python3
"""Root supervisor: genuine stock UID1000 child, RAM-only Android data writes."""
import errno
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import select
import subprocess
import time

import fuel_runtime as fr
import state
import health

BASE = Path('/run/frl/fuel'); RAM = BASE/'nvcfg'; ROOT = Path('/opt/frl-fuel-vendor')
CACHE = BASE/'production-prepared.json'
APEX_SHA = '94d6c7fcf4a58915c16bc88230994a305a0d159b84b8fddd91b3ae724f75f66d'
PAYLOAD_SHA = '98d0f48fb715bb81b11e470086611179c522134dd673f69d1a4d91e5382c2cd1'
ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C','LANG':'C'}
stop = False


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def verify_prepared(cache):
    state.require(cache['schema'] == 'FRL_FUEL_PREPARED_1' and cache['boot_id'] == boot_id(), 'stale preparation cache')
    state.require(fr.covering(BASE)['type'] == 'tmpfs' and fr.digest(BASE/'apex_payload.img') == PAYLOAD_SHA,
                  'APEX is not the pinned RAM image')
    row = fr.at(BASE/'apex')
    state.require(row and row['type'] == 'ext4' and {'ro','nosuid','nodev'} <= row['opts'], 'APEX mount differs')
    sysloop = Path('/sys/dev/block')/row['dev']
    state.require((sysloop/'loop').is_dir(), 'APEX not loop backed')
    node = fr.block_node(sysloop.resolve())
    imagefd = os.open(BASE/'apex_payload.img',os.O_RDONLY|os.O_NOFOLLOW)
    fd = os.open(node,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        fr.loop_verify(fd,imagefd)
    finally:
        os.close(fd); os.close(imagefd)
    for source,target,ro in [(fr.STOCK/'system/system',ROOT/'system',True),
                             (fr.STOCK/'vendor',ROOT/'vendor',True),
                             (BASE/'apex',ROOT/'apex/com.android.runtime',True),
                             (Path('/dev'),ROOT/'dev',False),(Path('/sys'),ROOT/'sys',False),
                             (Path('/proc'),ROOT/'proc',False)]:
        row = fr.at(target)
        state.require(row and os.path.samefile(source,target) and (not ro or 'ro' in row['opts']), 'runtime bind differs: '+str(target))
    row = fr.at(ROOT/'mnt/vendor/nvcfg'); cover = fr.covering(RAM); s = RAM.lstat()
    expected = str(PurePosixPath(cover['root'])/RAM.relative_to(cover['target']))
    state.require(cover['type'] == 'tmpfs' and row and row['type'] == 'tmpfs'
                  and {'rw','nosuid','nodev','noexec'} <= row['opts'] and row['dev'] == cover['dev']
                  and row['root'] == expected and os.path.samefile(RAM,ROOT/'mnt/vendor/nvcfg')
                  and [s.st_dev,s.st_ino] == cache['ram_identity'], 'RAM calibration bind differs')
    state.require(fr.digest(ROOT/'vendor/bin/fuelgauged') == health.DAEMON_SHA, 'daemon pin differs')


def prepare_once():
    if CACHE.exists() or CACHE.is_symlink():
        s = CACHE.lstat()
        state.require(not CACHE.is_symlink() and s.st_uid == 0 and s.st_mode & 0o777 == 0o600, 'unsafe cache')
        cache = json.loads(CACHE.read_text()); verify_prepared(cache); return
    # Any uncached prior preparation remains an explicit error in reviewed prepare.
    checkpoint = state.read_checkpoint()
    fr.prepare(APEX_SHA,PAYLOAD_SHA)
    state.restore_ram(checkpoint,RAM)
    target = ROOT/'mnt/vendor/nvcfg'; fr.directory(target)
    state.require(not fr.at(target) and not any(target.iterdir()), 'uncached calibration target exists')
    fr.run('mount','--bind',str(RAM),str(target))
    fr.run('mount','-o','remount,bind,rw,nosuid,nodev,noexec',str(target))
    s = RAM.stat(); cache = {'schema':'FRL_FUEL_PREPARED_1','boot_id':boot_id(),'ram_identity':[s.st_dev,s.st_ino]}
    verify_prepared(cache)
    state.atomic(CACHE,state.encode(cache))


def no_existing_daemon():
    for p in Path('/proc').iterdir():
        if not p.name.isdecimal():
            continue
        try:
            if health.proc(int(p.name))['state'] in ('Z','X'):
                continue
            comm = (p/'comm').read_text().strip()
            try:
                same = os.path.samefile(p/'exe',ROOT/'vendor/bin/fuelgauged')
            except FileNotFoundError:
                same = False
            state.require(not same and comm != 'fuelgauged','existing genuine daemon; no duplicate start')
        except FileNotFoundError:
            continue


def observe_line(identity,line):
    proof = identity['initialization']
    message = line.split(';',1)[-1]
    registration = re.search(r'FG_DAEMON_CMD_SET_DAEMON_PID\s*=\s*(\d+)',message)
    if registration and int(registration[1]) != identity['pid']:
        proof.clear(); proof['registered_pid'] = int(registration[1]); return
    if not re.search(r'\['+str(identity['pid'])+r':fuelgauged\]',message):
        return
    if registration:
        proof.clear(); proof['registered_pid'] = identity['pid']; proof['registration_record'] = line[:400]
    uisoc = re.search(r'FG_DAEMON_CMD_SET_KERNEL_UISOC\s*=\s*(-?\d+)\s+gm\.ui_soc:(-?\d+)\s+GM3:(\d+)',message)
    if uisoc and proof.get('registered_pid') == identity['pid'] and 0 <= int(uisoc[2]) <= 100:
        proof.update(uisoc_after_registration=True,gm30_disabled=int(uisoc[3]),uisoc_record=line[:400])
    if re.search(r'NVRAM_ready\s+1\b.*nv_fail\(0\)',message):
        proof['real_nvram_loaded'] = True


class LogGap(RuntimeError):
    pass


def consume(kmsg, identity):
    deadline=time.monotonic()+0.05
    for _ in range(4096):
        try:
            raw=os.read(kmsg,65536)
        except BlockingIOError:
            return
        except OSError as error:
            if error.errno==errno.EPIPE:
                identity['initialization'].clear()
                identity['initialization']['log_gap']=True
                raise LogGap('kernel log evidence overwritten; reinitialize owned daemon') from error
            raise
        if not raw:return
        if b'fuelgauged' in raw or b'FG_DAEMON_CMD_SET_DAEMON_PID' in raw:
            observe_line(identity,raw.decode(errors='replace'))
        if time.monotonic()>=deadline:return


def terminate_owned(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=3)


def run_child():
    no_existing_daemon()
    kmsg = os.open('/dev/kmsg',os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
    child = None
    try:
        os.lseek(kmsg,0,os.SEEK_END)
        state.require('GNU coreutils' in subprocess.run(['/usr/sbin/chroot','--version'],check=True,
                      capture_output=True,text=True,env=ENV).stdout,'GNU chroot required')
        child = subprocess.Popen(['/usr/sbin/chroot','--userspec=1000:1000','--groups=1000',str(ROOT),'/vendor/bin/fuelgauged'],
                                 stdin=subprocess.DEVNULL,env=ENV,cwd='/',close_fds=True,start_new_session=True)
        actual = health.proc(child.pid); parent = health.proc(os.getpid())
        identity = {'schema':'FRL_FUEL_CHILD_1','boot_id':boot_id(),'pid':child.pid,'start':actual['start'],
                    'supervisor_pid':os.getpid(),'supervisor_start':parent['start'],'initialization':{}}
        state.atomic(health.IDENTITY,state.encode(identity)); next_save = time.monotonic()+30; qualified = False; next_check = 0
        while not stop and child.poll() is None:
            try:
                consume(kmsg,identity)
            except LogGap:
                state.atomic(health.IDENTITY,state.encode(identity))
                raise
            if time.monotonic()>=next_check:
                state.atomic(health.IDENTITY,state.encode(identity))
                try:
                    qualified = health.initialized(identity) and health.process_valid(identity)
                except (OSError,ValueError,KeyError):
                    qualified = False
                next_check = time.monotonic()+0.5
            if time.monotonic() >= next_save:
                try:
                    if qualified:
                        generation = state.checkpoint(RAM)
                        print('FRL_FUEL_CHECKPOINT generation='+str(generation),flush=True)
                except (OSError,ValueError,RuntimeError) as e:
                    print('FRL_FUEL_CHECKPOINT_RETAINED '+str(e),flush=True)
                next_save = time.monotonic()+30
            select.select([kmsg],[],[],0.1)
        terminate_owned(child)
        try:
            if qualified and health.initialized(identity):
                state.checkpoint(RAM)
        except (OSError,ValueError,RuntimeError) as e:
            print('FRL_FUEL_FINAL_CHECKPOINT_RETAINED '+str(e),flush=True)
        return 0 if stop else 1
    finally:
        if child is not None:
            terminate_owned(child)
        os.close(kmsg)
        # The stale identity remains diagnostic evidence; health checks real PID/start.


def main():
    global stop
    state.require(os.geteuid() == 0,'root supervisor required')
    fr.directory(BASE,0o700)
    fd = os.open(BASE/'production.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        signal.signal(signal.SIGTERM,lambda *_:globals().__setitem__('stop',True))
        signal.signal(signal.SIGINT,lambda *_:globals().__setitem__('stop',True))
        prepare_once()
        return run_child() if not stop else 0
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('FRL_FUEL_REFUSED '+str(error),flush=True); raise SystemExit(78)
