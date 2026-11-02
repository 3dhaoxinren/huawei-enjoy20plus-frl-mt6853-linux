#!/usr/bin/python3
"""MT6853 native Mali dashboard. Fails closed; no Mesa/Pixman/software fallback."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import sys
import time
from canvas import Atlas
from dashboard import Dashboard
from engine import Engine
from detail_metrics import DetailSampler as Sampler
from terminal import Terminal
from touch import Touch

STOP=False
CAPTURE=False


def stop(*args):
    global STOP
    STOP=True


def capture(*args):
    global CAPTURE
    CAPTURE=True


def atomic(path,data):
    temp=path.with_suffix('.new')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o644)
    with os.fdopen(fd,'w') as f:
        json.dump(data,f)
    os.replace(temp,path)


def main():
    global CAPTURE
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',default=str(Path(__file__).parent/'frl-gpu-renderer'))
    parser.add_argument('--state',default='/run/frl/gpu-monitor')
    parser.add_argument('--test',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0 or Path('/etc/frl/runtime-mode').read_text().strip()!='server':
        raise RuntimeError('native root in server mode required')
    if Path('/run/frl/control/shutdown-in-progress').exists():
        raise RuntimeError('shutdown has priority')
    folder=Path(args.state)
    allowed=Path(__file__).resolve().parent/'state' if args.test else Path('/run/frl/gpu-monitor')
    if folder != allowed or folder.resolve() != folder:
        raise RuntimeError('state path outside fixed monitor directory')
    folder.mkdir(mode=0o755,exist_ok=True)
    if folder.is_symlink() or folder.stat().st_uid!=0 or folder.stat().st_mode&0o022:
        raise RuntimeError('untrusted state directory')
    lock=os.open('/run/frl/gpu-monitor.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    atomic(folder/'status.json',{'ready':False,'pid':os.getpid(),'monotonic':time.monotonic()})
    light=Path('/sys/class/leds/lcd-backlight/brightness')
    engine=touch=None
    terminal=Terminal('gui-test-gpu' if args.test else 'console')
    start=time.monotonic();sampler=Sampler();ui=Dashboard(Atlas(),terminal)
    test_step=0;last_sample=last_capture=0;dirty=True;data={};last_state=0
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    signal.signal(signal.SIGUSR1,capture)
    try:
        touch=Touch();engine=Engine(args.binary,folder/'renderer.log')
        if not args.test:
            terminal.attach()
        while not STOP:
            now=time.monotonic()
            if Path('/run/frl/control/shutdown-in-progress').exists():break
            if now-last_sample>=3:
                data=sampler.sample();last_sample=now;dirty=True
            if ui.tab=='terminal' and terminal.pane and now-last_capture>=.5:
                previous=(terminal.lines,terminal.cursor)
                terminal.capture();last_capture=now
                dirty=dirty or previous!=(terminal.lines,terminal.cursor)
            if select.select([touch.fd],[],[],0)[0]:
                touch.read()
                for phase,begin,end in touch.motions:
                    try:dirty=ui.pointer(phase,begin,end) or dirty
                    except Exception as exc:ui.error=str(exc)[:100];dirty=True
            try:dirty=ui.poll() or dirty
            except Exception as exc:ui.error=str(exc)[:100];dirty=True
            if dirty:
                data['render_frames']=engine.frames
                data['render_ms']=engine.draw_ms if engine.frames else None
                engine.draw(ui.render(data));dirty=False
            if CAPTURE:
                engine.display.screenshot(folder/'capture.png');CAPTURE=False
            if now-last_state>=1 or engine.frames==1:
                atomic(folder/'status.json',{'ready':True,'pid':os.getpid(),'worker_pid':engine.worker.pid,
                    'renderer':'Mali-G57 MC3','transport':'ION DMA-BUF / DRM page flip','frames':engine.frames,
                    'draw_ms':round(engine.draw_ms,2),'tab':ui.tab,'actions':ui.actions,'shell_pid':terminal.pid,
                    'touch_ranges':touch.ranges,'monotonic':now,'test':args.test,
                    'process_action':ui.process_actions.last_result,
                    'detail':ui.detail,'management_action':ui.management.result})
                last_state=now
            if args.test:
                elapsed=now-start
                if test_step==0 and elapsed>6:
                    Path('/sys/class/graphics/fb0/blank').write_text('0\n');time.sleep(1);light.write_text('800\n')
                    engine.display.screenshot(folder/'overview.png');ui.activate(('tab','processes'));dirty=True;test_step=1
                elif test_step==1 and elapsed>9:
                    engine.display.screenshot(folder/'processes.png');ui.activate(('tab','terminal'));dirty=True;test_step=2
                elif test_step==2 and elapsed>12:
                    # Exact same hit-testing and key encoding as touch, isolated test pane only.
                    for key in list('echo gpu')+['Enter']:
                        key='space' if key==' ' else key
                        box=next(box for box,action in ui.hits if action==('key',key))
                        point=(box[0]+box[2]/2,box[1]+box[3]/2)
                        assert ui.touch(point,point)
                    dirty=True;test_step=3
                elif test_step==3 and elapsed>15:
                    terminal.capture()
                    assert any(row.strip()=='gpu' for row in terminal.lines),terminal.lines
                    engine.display.screenshot(folder/'terminal.png')
                    ui.activate(('keyboard',None));dirty=True;test_step=4
                elif test_step==4 and elapsed>17:
                    engine.display.screenshot(folder/'terminal-expanded.png')
                    atomic(folder/'test.json',{'passed':True,'frames':engine.frames,'key_actions':ui.actions,
                                               'shell_pid':terminal.pid,'capture':terminal.lines})
                    break
            select.select([touch.fd],[],[],.05)
    finally:
        try:ui.cancel_slider()
        except Exception:pass  # Lease expires if power daemon is already stopping.
        light.write_text('0\n')
        try:
            if engine:engine.close()
        finally:
            if touch:touch.close()
            atomic(folder/'status.json',{'ready':False,'pid':os.getpid(),'monotonic':time.monotonic()})
            if args.test and terminal.pane:
                terminal.command('kill-session','-t','=gui-test-gpu',check=False)
            os.close(lock)


if __name__=='__main__':main()
