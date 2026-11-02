"""Local positive-PID SIGTERM, with fresh identity checks and guarded confirmation."""
import errno
import hashlib
import os
from pathlib import Path
import signal
import time

PROC = Path('/proc')
SERVICES = Path('/run/frl/service')
PROTECTED_SERVICES = {'desktop', 'power', 'power-terminal', 'fuel', 'charge-limit', 'aptouch'}
SUPERVISORS = {'frl-init', 'init', 'runsv', 'runsvdir', 'svlogd'}
SERVICE_IMPACT = {
    'ssh': ['SSH 连接可能立即中断。', '远程维护和当前远程命令可能停止。'],
    'wpa': ['Wi-Fi 连接可能中断，SSH 会掉线。', '后台联网任务可能失败。'],
    'dhcp': ['IP 地址续租可能停止，网络可能中断。'],
    'radio': ['无线硬件管理可能停止，Wi-Fi 可能失效。'],
    'apps': ['后台应用管理可能中断，应用可能退出。'],
}


class ProcessError(Exception):
    pass


def read_process(pid):
    if type(pid) is not int or pid <= 0:
        raise ProcessError('无效进程编号')
    path = PROC / str(pid)
    try:
        raw = (path/'stat').read_text()
        right = raw.rfind(')');fields = raw[right+2:].split()
        name = ''.join(c if c.isprintable() else '?' for c in raw[raw.find('(')+1:right])[:64]
        uid = path.stat().st_uid
        with (path/'cmdline').open('rb') as stream:cmd=stream.read(8192)
        try:exe = os.readlink(path/'exe')
        except OSError:exe = ''
        return {'pid':pid, 'name':name, 'state':fields[0], 'ppid':int(fields[1]),
                'flags':int(fields[6]), 'start':int(fields[19]), 'uid':uid,
                'exe':exe, 'cmd_hash':hashlib.sha256(cmd).hexdigest()}
    except (OSError, ValueError, IndexError):
        raise ProcessError('进程已退出或无法读取，请刷新列表') from None


def identity(info):
    return tuple(info[k] for k in ('pid','start','uid','exe','cmd_hash','name'))


def service_of(info):
    """Resolve supervised descendants too (e.g. wpa's wrapper -> supplicant)."""
    owners = {}
    for path in SERVICES.glob('*/supervise/pid'):
        try:
            pid = int(path.read_text().strip())
            if pid > 0:owners[pid] = path.parent.parent.name
        except (OSError, ValueError):pass
    visited = set();current = info
    for _ in range(32):
        pid = current['pid']
        if pid in owners:
            service=owners[pid]
            # An ordinary command launched over SSH is not the SSH service.
            if service=='ssh' and not info['name'].startswith('sshd'):return None
            return service
        if pid in visited or pid <= 1:break
        visited.add(pid)
        try:current = read_process(current['ppid'])
        except ProcessError:break
    return None


def classify(info):
    service = service_of(info)
    if (info['pid'] in (1,2,os.getpid()) or info['flags'] & 0x00200000
            or info['name'] in SUPERVISORS or service in PROTECTED_SERVICES):
        return 'protected', ['系统核心 / 硬件控制进程，禁止在此结束。',
                             '可能影响屏幕、触摸、供电或系统运行。']
    if info['state'] in ('Z','X'):
        return 'protected', ['该进程已退出，等待其父进程回收。']
    if service:
        return 'important', SERVICE_IMPACT.get(service, ['系统后台服务，对应功能可能中断。']) + ['服务管理器可能会自动重启它。']
    if info['name'].startswith(('tmux','bash','zsh','fish','sh')):
        return 'important', ['终端会话及其中运行的命令可能退出。', '尚未保存的终端工作可能丢失。']
    if info['uid'] == 0:
        return 'important', ['这是 root 权限进程，依赖它的功能可能中断。', '请确认用途并保存相关工作。']
    return 'normal', ['该程序将收到正常退出请求。', '尚未保存的工作可能丢失。']


class ProcessActions:
    def __init__(self):
        self.pending = None
        self.last_result = None
        self.next_poll = 0

    def select(self, row):
        info = read_process(row['pid'])
        if info['start'] != row.get('start'):
            raise ProcessError('列表已过期，进程编号已被复用，请刷新')
        risk, impact = classify(info)
        self.pending = {'target':info, 'risk':risk, 'impact':impact,
                        'stage':'blocked' if risk=='protected' else 'warning' if risk=='important' else 'confirm',
                        'ready_at':time.monotonic()+.5, 'message':None}

    def cancel(self):
        self.pending = None

    def advance(self):
        if not self.pending or self.pending['stage']!='warning':
            raise ProcessError('请先选择进程并阅读影响')
        if time.monotonic() < self.pending['ready_at']:
            raise ProcessError('请先阅读影响，再继续')
        self.pending['stage']='confirm'
        self.pending['ready_at']=time.monotonic()+1
        self.pending['armed']=False

    def _validate(self, previous, risk):
        current = read_process(previous['pid'])
        current_risk, _ = classify(current)
        if identity(current)!=identity(previous) or current_risk!=risk:
            raise ProcessError('进程身份或风险已变化，请取消后重新选择')
        if current_risk=='protected':
            raise ProcessError('该进程受系统保护')
        return current

    def terminate(self):
        pending = self.pending
        if not pending or pending['stage']!='confirm':
            raise ProcessError('缺少最终确认')
        if time.monotonic() < pending['ready_at']:
            raise ProcessError('请稍候，再单独确认结束')
        target=pending['target'];pid=target['pid'];fd=None
        try:
            self._validate(target,pending['risk'])
            try:fd=os.pidfd_open(pid)
            except AttributeError:pass
            except OSError as exc:
                if exc.errno not in (errno.ENOSYS,errno.EINVAL):raise
            # Revalidate after pidfd acquisition and immediately before signalling.
            self._validate(target,pending['risk'])
            if fd is not None:signal.pidfd_send_signal(fd,signal.SIGTERM)
            else:
                # MT6853's current 4.14 kernel has no pidfd. A small final
                # read-to-kill race remains; never claim this fallback is atomic.
                latest=read_process(pid)
                if identity(latest)!=identity(target):raise ProcessError('进程已变化，已取消结束')
                os.kill(pid,signal.SIGTERM)  # Positive single PID only, never a process group.
        except (OSError,ProcessError) as exc:
            pending['stage']='failed'
            pending['message']=str(exc) if isinstance(exc,ProcessError) else '进程已退出或没有结束权限'
            self.last_result={'pid':pid,'start':target['start'],'outcome':'not_sent','message':pending['message']}
            return
        finally:
            if fd is not None:os.close(fd)
        pending['stage']='sent';pending['message']='已发送 SIGTERM，正在检查退出状态…'
        pending['deadline']=time.monotonic()+3
        self.last_result={'pid':pid,'start':target['start'],'outcome':'signal_sent','signal':'SIGTERM'}
        self.next_poll=0

    def poll(self):
        p=self.pending
        if p and p['stage'] in ('warning','confirm'):
            if not p.get('armed') and time.monotonic()>=p['ready_at']:
                p['armed']=True;return True
            return False
        if not p or p['stage']!='sent' or time.monotonic()<self.next_poll:return False
        self.next_poll=time.monotonic()+.3
        target=p['target']
        try:
            current=read_process(target['pid'])
            exited=current['start']!=target['start'] or current['state'] in ('Z','X')
        except ProcessError:exited=True
        if exited or time.monotonic()>=p['deadline']:
            p['stage']='done'
            p['message']='所选进程已退出。' if exited else '进程仍在运行；未自动强制结束。'
            self.last_result['outcome']='exited' if exited else 'still_running'
            return True
        return False
