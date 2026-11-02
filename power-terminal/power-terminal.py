#!/usr/bin/python3 -I
"""On-demand local terminal for the persistent FRL server mode."""
import argparse,contextlib,fcntl,importlib.util,json,os,selectors,signal,socket,stat,struct,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,'/usr/lib/frl')
from server_common import atomic,guard,runtime_mode
sys.path.insert(0,'/usr/lib/frl/gpu-monitor')
from display_settings import load_settings, load_brightness, brightness_raw, validate_brightness, BRIGHTNESS_MAX

BASE=Path('/run/frl/power-terminal')
SERVICE=Path('/run/frl/service')
LIGHT=Path('/sys/class/leds/lcd-backlight/brightness')
BLANK=Path('/sys/class/graphics/fb0/blank')
SHUTDOWN=Path('/run/frl/control/shutdown-in-progress')
EVENT=struct.Struct('@llHHi')
STOP=False


def command(*args,timeout=16):
    return subprocess.run(args,capture_output=True,text=True,check=True,timeout=timeout)


@contextlib.contextmanager
def mode_lock():
    fd=os.open('/run/frl/server-mode.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:os.close(fd)


def permitted():
    if STOP or SHUTDOWN.exists():raise RuntimeError('shutdown has priority')
    if runtime_mode()!='server':raise RuntimeError('local server terminal requires server mode')


def wake_panel(brightness):
    check_brightness(brightness)
    permitted()
    # The vendor panel can remain off after DRM becomes active. Sysfs LED
    # brightness alone acknowledges a cached value even when hardware rejects it.
    LIGHT.write_text('0\n')
    BLANK.write_text('0\n')
    for _ in range(10):
        time.sleep(.1)
        permitted()
    LIGHT.write_text(str(brightness)+'\n')
    if int(LIGHT.read_text())!=brightness:raise RuntimeError('backlight readback differs')


def check_brightness(value):
    maximum=int((LIGHT.parent/'max_brightness').read_text())
    if maximum!=BRIGHTNESS_MAX or not 51<=value<=maximum:
        raise RuntimeError('backlight range differs from this panel')


class ShortPress:
    def __init__(self):self.pressed=None;self.last=-10
    def event(self,kind,code,value,stamp):
        if kind!=1 or code!=116:return False
        if value==1:
            if self.pressed is None:self.pressed=stamp
            return False
        if value!=0:return False
        start=self.pressed;self.pressed=None
        if start is None or not 0<=stamp-start<1.5 or stamp-self.last<.6:return False
        self.last=stamp
        return True


class Console:
    def __init__(self):
        self.visible=False;self.activity=time.monotonic();self.error=None;self.key_count=0
        self.idle_off_seconds,self.settings_error=load_settings()
        self.brightness_percent,self.brightness_error=load_brightness()
        self.applied_brightness=self.brightness_percent
        self.preview_percent=None;self.preview_until=0
        self.settings_checked=0
    def refresh_settings(self):
        now=time.monotonic()
        if now-self.settings_checked>=1:
            self.idle_off_seconds,self.settings_error=load_settings()
            self.brightness_percent,self.brightness_error=load_brightness()
            self.settings_checked=now
    def status(self):
        return {'visible':self.visible,'mode':runtime_mode(),'last_error':self.error,'short_presses':self.key_count,
                'brightness':int(LIGHT.read_text()),'idle_seconds':int(time.monotonic()-self.activity),
                'idle_off_seconds':self.idle_off_seconds,'display_settings_error':self.settings_error,'session':'tmux:frl-console/console','monotonic':time.monotonic(),
                'brightness_percent':self.brightness_percent,'brightness_settings_error':self.brightness_error,
                'brightness_preview':self.preview_percent}
    def publish(self):atomic(BASE/'status.json',json.dumps(self.status()).encode(),0o644)
    def hide_locked(self):
        errors=[]
        LIGHT.write_text('0\n')
        for name in ('desktop','seatd'):
            try:command('/usr/bin/sv','-w','5','down',str(SERVICE/name),timeout=7)
            except Exception as exc:errors.append(name+': '+str(exc))
        self.visible=False
        self.preview_percent=None
        if errors:raise RuntimeError('; '.join(errors))
    def hide(self):
        with mode_lock():permitted();self.hide_locked()
    def show(self):
        with mode_lock():
            permitted()
            self.preview_percent=None
            try:
                for name in ('seatd','desktop'):
                    permitted();command('/usr/bin/sv','up',str(SERVICE/name))
                deadline=time.monotonic()+25
                while True:
                    permitted()
                    try:
                        gpu=json.loads(Path('/run/frl/gpu-monitor/status.json').read_text())
                        if not gpu.get('ready') or gpu.get('renderer')!='Mali-G57 MC3' or gpu.get('frames',0)<1:
                            raise RuntimeError('GPU not ready')
                        if not 0<=time.monotonic()-gpu['monotonic']<5:raise RuntimeError('stale GPU status')
                        pid=int(gpu['pid']);os.kill(pid,0)
                        cmdline=Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\0')
                        if b'/usr/lib/frl/gpu-monitor/monitor.py' not in cmdline:raise RuntimeError('GPU PID differs')
                        command('/usr/bin/chpst','-u','frl:frl','/usr/bin/tmux','-L','frl-console','has-session','-t','console',timeout=3)
                        break
                    except Exception:
                        if time.monotonic()>=deadline:raise RuntimeError('Mali monitor readiness deadline; no CPU fallback')
                        time.sleep(.2)
                permitted()
                self.brightness_percent,self.brightness_error=load_brightness()
                brightness=brightness_raw(self.brightness_percent)
                wake_panel(brightness)
                self.applied_brightness=self.brightness_percent
                self.visible=True;self.activity=time.monotonic();self.error=None
            except BaseException:
                # Mode lock prevents an unrelated desktop transition racing cleanup.
                if not SHUTDOWN.exists():self.hide_locked()
                raise
    def toggle(self):
        self.hide() if self.visible else self.show()
    def apply_brightness(self,percent):
        if not self.visible:return
        with mode_lock():
            permitted()
            # A preview must never unblank a panel or change desktop mode.
            if int(LIGHT.read_text())>0:
                value=brightness_raw(percent);check_brightness(value)
                LIGHT.write_text(str(value)+'\n')
                if int(LIGHT.read_text())!=value:raise RuntimeError('backlight readback differs')
                self.applied_brightness=percent
    def preview(self,percent):
        percent=validate_brightness(percent)
        if not self.visible:raise RuntimeError('display is off')
        self.apply_brightness(percent)
        self.preview_percent=percent;self.preview_until=time.monotonic()+3
        self.activity=time.monotonic()
    def clear_preview(self):
        self.preview_percent=None
        self.brightness_percent,self.brightness_error=load_brightness()
        self.apply_brightness(self.brightness_percent)
    def tick(self):
        self.refresh_settings()
        if runtime_mode()!='server':self.visible=False;return
        if self.visible:
            state=command('/usr/bin/sv','status',str(SERVICE/'desktop')).stdout
            if not state.startswith('run:'):self.visible=False;LIGHT.write_text('0\n')
            elif self.idle_off_seconds>0 and time.monotonic()-self.activity>=self.idle_off_seconds:self.hide()
            else:
                if time.monotonic()>=self.preview_until:self.preview_percent=None
                desired=self.brightness_percent if self.preview_percent is None else self.preview_percent
                if self.applied_brightness!=desired:self.apply_brightness(desired)


def open_input(node,selector):
    name=(node/'device/name').read_text().strip()
    if name not in ('mtk-kpd','input_mt_wrapper'):return None
    major,minor=map(int,(node/'dev').read_text().split(':'));path=Path('/dev/input')/node.name
    fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        if not stat.S_ISCHR(info.st_mode) or info.st_rdev!=os.makedev(major,minor):raise RuntimeError('input identity differs')
        # EVIOCSCLOCKID: compare physical event timestamps with monotonic time.
        fcntl.ioctl(fd,0x400445a0,struct.pack('i',1))
        selector.register(fd,selectors.EVENT_READ,name)
        return fd,name
    except BaseException:os.close(fd);raise


def stop(*args):
    global STOP
    STOP=True


def serve():
    guard();BASE.mkdir(mode=0o755,exist_ok=True)
    lock=os.open(BASE/'service.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    endpoint=BASE/'control.sock'
    if endpoint.exists():
        if not stat.S_ISSOCK(endpoint.lstat().st_mode):raise RuntimeError('unexpected control path')
        endpoint.unlink()
    server=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);server.bind(str(endpoint));os.chmod(endpoint,0o600)
    server.listen(2);server.setblocking(False);selector=selectors.DefaultSelector();selector.register(server,selectors.EVENT_READ,'client')
    inputs=[item for node in Path('/sys/class/input').glob('event*') if (item:=open_input(node,selector))]
    if sorted(n for _,n in inputs)!=['input_mt_wrapper','mtk-kpd']:raise RuntimeError('required input devices absent')
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    console=Console();press=ShortPress();published=0
    # Reconcile a listener crash without changing the persistent desktop mode.
    if runtime_mode()=='server' and not SHUTDOWN.exists():
        with mode_lock():console.hide_locked()
    try:
        while not STOP and not SHUTDOWN.exists():
            for key,_ in selector.select(timeout=.5):
                if key.data=='client':
                    conn,_=server.accept()
                    with conn:
                        conn.settimeout(.5)
                        try:
                            _,uid,_=struct.unpack('3i',conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                            if uid!=0:raise PermissionError('root management required')
                            op=conn.recv(64).decode()
                            if op=='show':console.show()
                            elif op=='hide':console.hide()
                            elif op=='toggle':console.toggle()
                            elif op.startswith('brightness-preview '):console.preview(float(op.split(' ',1)[1]))
                            elif op=='brightness-clear':console.clear_preview()
                            elif op!='status':raise ValueError('unknown operation')
                            result={'ok':True,**console.status()}
                        except Exception as exc:console.error=str(exc);result={'ok':False,'error':str(exc)}
                        try:conn.send(json.dumps(result).encode())
                        except OSError:pass
                else:
                    data=os.read(key.fd,EVENT.size*64)
                    if not data or len(data)%EVENT.size:raise RuntimeError('input stream invalid')
                    for sec,usec,kind,code,value in EVENT.iter_unpack(data):
                        stamp=sec+usec/1000000
                        if abs(time.monotonic()-stamp)>2:continue
                        if key.data=='mtk-kpd' and press.event(kind,code,value,stamp):
                            if runtime_mode()=='server':
                                console.key_count+=1
                                try:console.toggle();console.error=None
                                except Exception as exc:console.error=str(exc)
                                print(json.dumps({'event':'power_short_press',**console.status()}),flush=True)
                        elif console.visible and (kind==3 or kind==1 and value==1):console.activity=time.monotonic()
            try:console.tick()
            except Exception as exc:console.error=str(exc)
            if time.monotonic()-published>2:console.publish();published=time.monotonic()
    finally:
        if not SHUTDOWN.exists() and runtime_mode()=='server':
            try:
                with mode_lock():console.hide_locked()
            except Exception as exc:print('cleanup: '+str(exc),flush=True)
        for fd,_ in inputs:os.close(fd)
        selector.close();server.close();endpoint.unlink(missing_ok=True);os.close(lock)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['serve','status','show','hide','toggle']);args=p.parse_args()
    if args.action=='serve':serve();return
    guard()
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as client:
        client.settimeout(40);client.connect(str(BASE/'control.sock'));client.send(args.action.encode());result=json.loads(client.recv(4096))
    print(json.dumps(result))
    if not result['ok']:raise SystemExit(1)


if __name__=='__main__':main()
