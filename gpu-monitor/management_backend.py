"""Fixed local management operations. No shell, new listener, or arbitrary command."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

APPS = Path('/etc/frl/apps')
CHARGE = Path('/etc/frl/charge-limit.json')
CHARGE_STATUS = Path('/run/frl/charge-limit/status.json')
RUNTIME = Path('/run/frl/gpu-management')
CLI_APP = '/usr/local/sbin/frl-app'
CLI_CHARGE = '/usr/local/sbin/frl-charge-limit'


class ManagementError(RuntimeError):
    pass


def clean(value, limit=200):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))[:limit]


def valid_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', name):
        raise ManagementError('应用名称无效')
    return name


def safe_directory(path):
    for p in (Path(path), *Path(path).parents):
        s = p.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
            raise ManagementError('目录权限不可信')


def trusted_bytes(path, limit=65536):
    path = Path(path)
    safe_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022 or s.st_nlink != 1 or s.st_size > limit:
            raise ManagementError('配置文件不可信或过大')
        data = os.read(fd, limit+1)
        if len(data) > limit:raise ManagementError('文件过大')
        return data
    finally:os.close(fd)


def config(path):
    raw = trusted_bytes(path)
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def charge_config():
    c, digest = config(CHARGE)
    if set(c) != {'schema', 'enabled', 'stop_percent', 'resume_percent'} or type(c['schema']) is not int or c['schema'] != 1 or type(c['enabled']) is not bool:
        raise ManagementError('充电配置格式无效')
    validate_thresholds(c['stop_percent'], c['resume_percent'])
    return c, digest


def validate_thresholds(stop, resume):
    if type(stop) is not int or type(resume) is not int or not 50 <= stop <= 95 or not 40 <= resume <= stop-3:
        raise ManagementError('停充 50–95%，恢复至少低 3%，且不低于 40%')


def app_config(name):
    c, digest = config(APPS/(valid_name(name)+'.json'))
    if set(c) != {'name', 'enabled', 'cwd', 'argv'} or c['name'] != name or type(c['enabled']) is not bool:
        raise ManagementError('应用配置格式无效')
    if not isinstance(c['argv'], list) or not c['argv'] or any(not isinstance(v, str) or '\0' in v for v in c['argv']):
        raise ManagementError('应用命令格式无效')
    if not c['argv'][0].startswith('/') or not isinstance(c['cwd'], str):
        raise ManagementError('应用路径无效')
    cwd = Path(c['cwd']).resolve(strict=True)
    if not cwd.is_dir() or not cwd.is_relative_to('/srv/frl'):
        raise ManagementError('应用必须位于 /srv/frl')
    return c, digest


def command(args, timeout=35):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, close_fds=True)
    if r.returncode:
        raise ManagementError('命令未确认成功，请刷新核对实际状态：'+clean(r.stderr or r.stdout, 110))
    return r.stdout[:65536]


def app_snapshot():
    safe_directory(APPS)
    rows = []
    paths = sorted(APPS.glob('*.json'))
    for p in paths[:32]:
        try:
            c, digest = app_config(p.stem)
            rows.append({'name': c['name'], 'enabled': c['enabled'], 'sha256': digest,
                         'status': '未知', 'running': None})
        except (OSError, ValueError, ManagementError):
            rows.append({'name': clean(p.stem, 32), 'enabled': None, 'sha256': None,
                         'status': '配置不可用 · 禁止操作', 'running': None})
    valid = [r for r in rows if r['sha256']]
    if valid:
        args = ['/usr/bin/sv', 'status']+['/run/frl/apps/'+r['name'] for r in valid]
        result = subprocess.run(args, capture_output=True, text=True, timeout=5, close_fds=True)
        for row in valid:
            pattern = r'^(run|down): /run/frl/apps/'+re.escape(row['name'])+r': ([^\n;]*)'
            match = re.search(pattern, result.stdout, re.M)
            if match:
                row['running'] = match[1] == 'run'
                row['status'] = clean(match[0].split(': ', 2)[-1], 90)
    return {'rows': rows, 'truncated': len(paths) > 32, 'stamp': time.monotonic()}


def power_snapshot():
    c, digest = charge_config()
    runtime = None
    try:
        raw, _ = config(CHARGE_STATUS)
        age = time.monotonic()-float(raw['monotonic'])
        if 0 <= age <= 15 and raw.get('config') == c:
            limits={'soc':(0,100),'battery_c':(-30,100),'battery_uv':(0,6000000),
                    'battery_ma':(-20000,20000),'vbus_mv':(0,30000),'ibus_ma':(-20000,20000)}
            sample=raw.get('sample',{})
            if not isinstance(sample,dict):raise ValueError('invalid sample')
            normalized={}
            for key,(low,high) in limits.items():
                value=sample.get(key)
                normalized[key]=value if type(value) in (int,float) and math.isfinite(value) and low<=value<=high else None
            runtime={'monotonic':raw['monotonic'],'state':clean(raw.get('state','未知状态'),80),'sample':normalized}
    except (OSError, ValueError, TypeError, KeyError, ManagementError):pass
    return {'config': c, 'sha256': digest, 'runtime': runtime, 'stamp': time.monotonic()}


def app_logs(name):
    app_config(name)
    p = Path('/var/log/frl/apps')/valid_name(name)/'current'
    safe_directory(p.parent)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
            raise ManagementError('日志文件不可信')
        os.lseek(fd, max(0, s.st_size-16384), os.SEEK_SET)
        return [clean(line, 180) for line in os.read(fd, 16384).decode('utf-8', 'replace').splitlines()[-60:]]
    finally:os.close(fd)


def private_runtime():
    safe_directory(RUNTIME.parent)
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    safe_directory(RUNTIME)
    if RUNTIME.stat().st_mode & 0o077:raise ManagementError('操作目录必须为私有目录')


def atomic_status(value):
    private_runtime()
    fd, name = tempfile.mkstemp(prefix='.job-', dir=RUNTIME)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush();os.fsync(stream.fileno())
        os.replace(name, RUNTIME/'status.json')
    finally:
        if os.path.exists(name):os.unlink(name)


def job_status():
    try:
        value, _ = config(RUNTIME/'status.json')
        if value.get('phase') not in ('running', 'done', 'failed') or not re.fullmatch('[a-f0-9]{32}', value.get('job', '')):
            raise ManagementError('操作状态格式无效')
        return value
    except FileNotFoundError:return None


def perform(request):
    if not isinstance(request, dict) or request.get('op') not in ('charge', 'app'):
        raise ManagementError('不支持的操作')
    if not re.fullmatch('[a-f0-9]{64}', str(request.get('expected', ''))):
        raise ManagementError('缺少配置身份')
    if Path('/run/frl/control/shutdown-in-progress').exists():
        raise ManagementError('系统正在关机')
    if request['op'] == 'charge':
        if set(request) != {'job','op','expected','stop','resume'}:raise ManagementError('操作字段无效')
        validate_thresholds(request['stop'], request['resume'])
        before, digest = charge_config()
        if digest != request['expected']:raise ManagementError('设置已变化，请刷新后重新确认')
        command([CLI_CHARGE, 'configure', '--stop', str(request['stop']), '--resume', str(request['resume'])])
        after, _ = charge_config()
        if after != {**before, 'stop_percent': request['stop'], 'resume_percent': request['resume']}:
            raise ManagementError('配置读回不一致，请刷新核对')
        return '阈值已保存，已核对配置；实际充电状态见电源页'
    if set(request) != {'job','op','expected','name','verb'} or request['verb'] not in ('enable', 'disable', 'restart'):
        raise ManagementError('操作字段或动作无效')
    name = valid_name(request['name'])
    before, digest = app_config(name)
    if digest != request['expected']:raise ManagementError('应用配置已变化，请刷新后重新确认')
    if request['verb'] == 'restart' and not before['enabled']:
        raise ManagementError('应用已停用，请使用启用')
    command([CLI_APP, request['verb'], name])
    after, _ = app_config(name)
    desired = request['verb'] != 'disable'
    if after != {**before, 'enabled': desired}:
        raise ManagementError('应用配置读回不一致')
    rows = app_snapshot()['rows']
    row = next((r for r in rows if r['name'] == name), None)
    if not row or row['running'] != desired:
        raise ManagementError('配置已保存，运行状态未达到目标；请查看日志')
    return '已核对应用状态；'+('开机自动启动已启用' if desired else '已停止并关闭开机启动')


def run_job(request):
    private_runtime()
    job = request.get('job') if isinstance(request, dict) else None
    if not isinstance(job, str) or not re.fullmatch('[a-f0-9]{32}', job):
        raise ManagementError('操作编号无效')
    fd = os.open(RUNTIME/'operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = job_status()
        if old and old['job'] == job:raise ManagementError('操作编号已执行，拒绝重放')
        result = {'job': job, 'phase': 'running', 'message': '正在执行，请勿重复操作',
                  'started': time.monotonic(), 'pid': os.getpid(), 'op': request.get('op')}
        atomic_status(result)
        try:
            result['message'] = perform(request)
            result['phase'] = 'done'
        except Exception as exc:
            result['phase'] = 'failed';result['message'] = clean(exc, 180)
        result['finished'] = time.monotonic();atomic_status(result)
        return result
    finally:os.close(fd)
