import json,os,stat,tempfile
from pathlib import Path

MODE=Path('/etc/frl/runtime-mode')
GRAPHICAL=('desktop','seatd','power')

def runtime_mode():
    if not MODE.exists() and not MODE.is_symlink():return 'desktop'
    fd=os.open(MODE,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_nlink!=1 or stat.S_IMODE(s.st_mode)!=0o600:
            raise RuntimeError('runtime-mode must be a root-owned regular file, mode0600')
        value=os.read(fd,32)
        if value not in (b'desktop\n',b'server\n'):raise RuntimeError('invalid runtime mode')
        return value.decode().strip()
    finally:os.close(fd)

def atomic(path,data,mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.'+path.name+'-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as f:
            os.fchmod(f.fileno(),mode);f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def ssh_listening():
    rows=Path('/proc/net/tcp').read_text().splitlines()[1:]
    return any(row.split()[1].split(':')[1]=='0016' and row.split()[3]=='0A' for row in rows)

def guard():
    if os.geteuid()!=0:raise PermissionError('native root required')
    if Path('/proc/1/comm').read_text().strip()!='frl-init':raise RuntimeError('native frl-init required')
    if Path('/sys/firmware/devicetree/base/compatible').read_bytes()!=b'mediatek,MT6853\0':raise RuntimeError('wrong board')
