"""Bounded local wpa_supplicant control and credential handling; no shell or logs."""
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import tempfile
import time
import uuid

CONTROL = Path('/run/wpa_supplicant/wlan0')
RUNTIME = Path('/run/frl/gpu-wifi')
CONFIG = Path('/etc/wpa_supplicant/frl-wlan0.conf')


class WifiError(Exception):
    """Only fixed, non-secret messages may reach the UI."""


def private_runtime():
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    s = RUNTIME.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o077:
        raise WifiError('Wi-Fi 管理目录权限异常')


def atomic_file(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix='.frl-wifi-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


class Control:
    def __init__(self):
        private_runtime()
        s = CONTROL.lstat()
        if not stat.S_ISSOCK(s.st_mode) or s.st_uid != 0:
            raise WifiError('Wi-Fi 服务不可用')
        self.local = RUNTIME / ('ctrl-' + uuid.uuid4().hex)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.settimeout(2)
        try:
            self.socket.bind(str(self.local));os.chmod(self.local, 0o600)
            self.socket.connect(str(CONTROL))
        except BaseException:
            self.close();raise

    def request(self, command):
        if not isinstance(command, str) or '\n' in command or '\r' in command or '\0' in command:
            raise WifiError('无效的 Wi-Fi 请求')
        raw = command.encode('ascii')
        if len(raw) > 2048:raise WifiError('Wi-Fi 请求过长')
        try:
            self.socket.send(raw)
            deadline=time.monotonic()+2
            while True:
                self.socket.settimeout(max(.01,deadline-time.monotonic()))
                result = self.socket.recv(65536)
                if not result.startswith(b'<'):break
                if time.monotonic()>=deadline:raise TimeoutError()
        except OSError:
            raise WifiError('Wi-Fi 服务响应超时') from None
        if result.startswith(b'FAIL') or result.startswith(b'UNKNOWN'):
            raise WifiError('Wi-Fi 服务拒绝操作')
        return result.rstrip(b'\n')

    def scan_wait(self,timeout=10):
        """Wait for scan completion, rather than presenting a stale cached list as new."""
        self.request('ATTACH')
        try:
            try:self.request('SCAN')
            except WifiError:pass  # An existing scan also sends completion to subscribers.
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                if not select.select([self.socket],[],[],max(0,deadline-time.monotonic()))[0]:break
                event=self.socket.recv(65536)
                if b'CTRL-EVENT-SCAN-RESULTS' in event:return True
                if b'CTRL-EVENT-SCAN-FAILED' in event:return False
            return False
        finally:
            try:self.request('DETACH')
            except WifiError:pass

    def close(self):
        self.socket.close();self.local.unlink(missing_ok=True)

    def __enter__(self):return self
    def __exit__(self, *args):self.close()


def unescape(value):
    result = bytearray();i = 0
    while i < len(value):
        if value[i] == 92 and i + 1 < len(value):
            if value[i+1:i+2] == b'x' and i + 3 < len(value):
                try:result.append(int(value[i+2:i+4],16));i+=4;continue
                except ValueError:pass
            replacements = {92:92,34:34,110:10,114:13,116:9,101:27}
            if value[i+1] in replacements:
                result.append(replacements[value[i+1]]);i+=2;continue
        result.append(value[i]);i+=1
    return bytes(result)


def display_name(raw):
    return ''.join(ch if ch.isprintable() else '�' for ch in raw.decode('utf-8','replace'))


def security_type(flags):
    if re.search(r'\[(?:WPA2|RSN)-PSK(?:[-+\]])', flags):return 'psk'
    if '[ESS]' in flags and not any(s in flags for s in ('WPA','RSN','WEP','OWE','DPP','EAP')):return 'open'
    return 'unsupported'


def status(control):
    fields = dict(line.split(b'=',1) for line in control.request('STATUS').splitlines() if b'=' in line)
    raw = unescape(fields.get(b'ssid',b''))
    return {'connected':fields.get(b'wpa_state')==b'COMPLETED','ssid_hex':raw.hex(),
            'ssid':display_name(raw),'ip':fields.get(b'ip_address',b'').decode('ascii','replace')}


def parse_scan(data, current='', saved=()):
    result = {}
    for row in data.splitlines()[1:401]:
        parts = row.split(b'\t',4)
        if len(parts) != 5 or not re.fullmatch(rb'(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}',parts[0]):continue
        try:frequency, strength = int(parts[1]), int(parts[2])
        except ValueError:continue
        raw = unescape(parts[4])
        if not 1 <= len(raw) <= 32 or not -150 <= strength <= 0:continue
        security = security_type(parts[3].decode('ascii','replace'))
        key = (raw.hex(),security)
        item = {'ssid':display_name(raw),'ssid_hex':raw.hex(),'security':security,'strength':strength,
                'frequency':frequency,'connected':raw.hex()==current,'saved':raw.hex() in saved}
        if key not in result or result[key]['strength']<strength:result[key]=item
    return sorted(result.values(),key=lambda r:(not r['connected'],-r['strength'],r['ssid']))[:100]


def saved_ssids(control):
    result=set()
    for row in control.request('LIST_NETWORKS').splitlines()[1:101]:
        parts=row.split(b'\t')
        if len(parts)>=4:result.add(unescape(parts[1]).hex())
    return result


def credential_bytes():
    parent=CONFIG.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=0 or parent.st_mode&0o022:
        raise WifiError('Wi-Fi 配置目录权限异常')
    fd=os.open(CONFIG,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or info.st_size>65536:
            raise WifiError('Wi-Fi 配置文件权限异常')
        data=os.read(fd,65537)
        if len(data)>65536:raise WifiError('Wi-Fi 配置文件过大')
        return data
    finally:os.close(fd)


def sections(config):
    pieces=[];outside=[];block=None
    for line in config.splitlines(keepends=True):
        stripped=line.strip()
        if re.fullmatch(rb'network\s*=\s*\{',stripped):
            if block is not None:raise WifiError('现有 Wi-Fi 配置格式不支持')
            if outside:pieces.append((False,b''.join(outside)));outside=[]
            block=[line]
        elif block is not None:
            block.append(line)
            if stripped==b'}':pieces.append((True,b''.join(block)));block=None
        else:outside.append(line)
    if block is not None:raise WifiError('现有 Wi-Fi 配置未闭合')
    if outside:pieces.append((False,b''.join(outside)))
    return pieces


def field(block,name):
    match=re.search(rb'(?m)^\s*'+name+rb'\s*=\s*([^\r\n]+)',block)
    return match[1].strip() if match else b''


def decode_ssid(value):
    if value.startswith(b'"') and value.endswith(b'"'):return unescape(value[1:-1])
    try:return bytes.fromhex(value.decode('ascii'))
    except (ValueError,UnicodeError):return b''


def derive_psk(password,ssid):
    encoded=password.encode('utf-8')
    if len(encoded)==64 and re.fullmatch(rb'[0-9a-fA-F]{64}',encoded):return encoded.decode().lower()
    if not 8<=len(encoded)<=63:raise WifiError('密码需为 8–63 字节，或 64 位十六进制密钥')
    return hashlib.pbkdf2_hmac('sha1',encoded,ssid,4096,32).hex()


def profile(config,ssid,password,security):
    pieces=sections(config);old=None;priorities=[0]
    for is_network,block in pieces:
        if not is_network:continue
        if decode_ssid(field(block,b'ssid'))==ssid:old=block
        value=field(block,b'priority')
        if re.fullmatch(rb'-?[0-9]+',value):priorities.append(int(value))
    priority=min(2147483647,max(priorities)+1)
    psk=None
    if security=='psk':
        if password:psk=derive_psk(password,ssid)
        elif old:
            value=field(old,b'psk')
            if re.fullmatch(rb'[0-9a-fA-F]{64}',value):psk=value.decode().lower()
            elif value.startswith(b'"') and value.endswith(b'"'):
                try:psk=derive_psk(unescape(value[1:-1]).decode('utf-8'),ssid)
                except UnicodeError:raise WifiError('已保存密码格式不支持，请重新输入') from None
        if psk is None:raise WifiError('此网络需要密码')
    elif security!='open':raise WifiError('暂不支持此 Wi-Fi 加密方式')
    new=b'network={\n    ssid='+ssid.hex().encode()+b'\n'
    new+=b'    key_mgmt='+ (b'WPA-PSK\n    proto=RSN\n    psk='+psk.encode()+b'\n' if psk else b'NONE\n')
    new+=b'    priority='+str(priority).encode()+b'\n}\n'
    retained=b''.join(block for is_network,block in pieces if not is_network or decode_ssid(field(block,b'ssid'))!=ssid)
    return retained.rstrip()+b'\n'+new,psk,priority
