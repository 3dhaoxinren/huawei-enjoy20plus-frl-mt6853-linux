#!/usr/bin/python3 -I
"""Consistent application-data backup with checksum verification and staged restore."""
import argparse,contextlib,fcntl,hashlib,json,os,re,signal,stat,subprocess,sys,tarfile,tempfile,time,uuid
from pathlib import Path,PurePosixPath
sys.path.insert(0,'/usr/lib/frl')
from server_common import atomic,guard

STORE=Path('/var/backups/frl/server-data')
ROOTS=['srv/frl/data','etc/frl/apps','etc/frl/runtime-mode','etc/frl/charge-limit.json','etc/frl/network/wlan-mac']
LIVE=Path('/run/frl/apps')
JOURNAL=Path('/run/frl/backup-recovery.json')
LOCK=Path('/run/frl/backup.lock')
MODE_LOCK=Path('/run/frl/server-mode.lock')
SHUTDOWN=Path('/run/frl/control/shutdown-in-progress')


@contextlib.contextmanager
def locked(path):
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:os.close(fd)


def save_pending(names,errors=None):
    atomic(JOURNAL,json.dumps({'pending':names,'errors':errors or {},'updated':time.time()}).encode())


def recover_pending():
    if not JOURNAL.exists():return {'pending':[],'errors':{}}
    names=json.loads(JOURNAL.read_text())['pending']
    if any(not re.fullmatch('[a-z][a-z0-9-]{0,31}',n) for n in names):raise ValueError('invalid recovery journal')
    pending=list(names);errors={}
    for name in names:
        try:
            with locked(MODE_LOCK):
                if SHUTDOWN.exists():raise RuntimeError('shutdown has priority; recovery deferred')
                config=json.loads((Path('/etc/frl/apps')/(name+'.json')).read_text())
                if config['enabled']:
                    subprocess.run(['/usr/bin/sv','-w','4','up',str(LIVE/name)],check=True,timeout=6,stdout=subprocess.DEVNULL)
                pending.remove(name)
        except Exception as exc:errors[name]=str(exc)
        save_pending(pending,errors)
    return {'pending':pending,'errors':errors}


def recover():
    with locked(LOCK):return recover_pending()


def trusted_directory(path):
    if not path.is_absolute() or '..' in path.parts:raise ValueError('absolute trusted destination required')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
            info=os.fstat(fd)
            if info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o022:
                raise ValueError('destination ancestors must be root-owned and not group/world writable')
        return fd
    except BaseException:os.close(fd);raise

def allowed(name):
    p=PurePosixPath(name)
    return not p.is_absolute() and '..' not in p.parts and any(name==r or name.startswith(r+'/') for r in ROOTS)

def restore(archive,destination):
    # Ancestors must prevent other users from replacing the new staging tree.
    parent=trusted_directory(destination.parent)
    try:os.mkdir(destination.name,mode=0o700,dir_fd=parent)
    finally:os.close(parent)
    with tarfile.open(archive,'r:gz') as tar:
        members=tar.getmembers()
        if len(members)>100000:raise ValueError('archive has too many entries')
        manifests=[m for m in members if m.name=='backup-manifest.json' and m.isfile() and m.size<16*1024*1024]
        if len(manifests)!=1:raise ValueError('missing or duplicate manifest')
        meta=json.load(tar.extractfile(manifests[0]));seen=set()
        for m in members:
            if m.name=='backup-manifest.json':continue
            if not allowed(m.name) or not (m.isdir() or m.isfile()) or m.name in seen:
                raise ValueError('unsafe archive entry')
            seen.add(m.name)
            out=destination/m.name
            if m.isdir():out.mkdir(parents=True,exist_ok=True,mode=0o700);continue
            out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            digest=hashlib.sha256();size=0
            fd=os.open(out,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as target,tar.extractfile(m) as src:
                while block:=src.read(1024*1024):target.write(block);digest.update(block);size+=len(block)
            if meta['files'].get(m.name)!={'sha256':digest.hexdigest(),'bytes':size}:raise ValueError('backup checksum mismatch: '+m.name)
        if set(meta['files'])!={m.name for m in members if m.isfile() and m.name!='backup-manifest.json'}:
            raise ValueError('manifest/member mismatch')
    return {'verified_files':len(meta['files']),'destination':str(destination)}

def verify(archive):
    with tempfile.TemporaryDirectory(prefix='.verify-',dir=STORE) as d:
        return restore(archive,Path(d)/'restored')

def create():
    STORE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with locked(LOCK):return create_locked()


def create_locked():
    recovery=recover_pending()
    if recovery['pending']:raise RuntimeError('application recovery still pending: '+json.dumps(recovery))
    if SHUTDOWN.exists():raise RuntimeError('shutdown is in progress')
    paused=[];temporary=None
    try:
        for path in LIVE.glob('*'):
            if not path.is_dir():continue
            state=subprocess.run(['/usr/bin/sv','status',str(path)],capture_output=True,text=True,timeout=4).stdout
            if state.startswith('run:'):
                with locked(MODE_LOCK):
                    if SHUTDOWN.exists():raise RuntimeError('shutdown is in progress')
                    paused.append(path.name);save_pending(paused)
                    subprocess.run(['/usr/bin/sv','-w','4','down',str(path)],check=True,timeout=6,stdout=subprocess.DEVNULL)
        name='frl-server-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.tar.gz'
        fd,temporary=tempfile.mkstemp(prefix='.backup-',dir=STORE);os.close(fd);os.chmod(temporary,0o600)
        meta={'schema':1,'created':time.time(),'files':{}}
        with tarfile.open(temporary,'w:gz') as tar:
            paths=[]
            for root in ROOTS:
                p=Path('/')/root
                if p.exists():paths.extend([p,*p.rglob('*')] if p.is_dir() else [p])
            for p in sorted(set(paths)):
                if p.is_symlink():raise RuntimeError('backup scope contains symlink: '+str(p))
                relative=str(p.relative_to('/'));info=tar.gettarinfo(str(p),arcname=relative)
                if p.is_dir():tar.addfile(info);continue
                if not p.is_file():raise ValueError('only regular data files are supported')
                digest=hashlib.sha256()
                fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
                class Reader:
                    def __init__(self,stream):self.stream=stream
                    def read(self,n):
                        block=self.stream.read(n);digest.update(block);return block
                with os.fdopen(fd,'rb') as source:tar.addfile(info,Reader(source))
                meta['files'][relative]={'sha256':digest.hexdigest(),'bytes':info.size}
            import io
            data=json.dumps(meta,indent=2).encode();info=tarfile.TarInfo('backup-manifest.json');info.size=len(data);info.mode=0o600
            tar.addfile(info,io.BytesIO(data))
        with open(temporary,'rb') as f:os.fsync(f.fileno())
        result=verify(temporary)
        final=STORE/name;os.replace(temporary,final);temporary=None
        fd=os.open(STORE,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        archives=sorted(STORE.glob('frl-server-*.tar.gz'),key=lambda p:p.stat().st_mtime,reverse=True)
        for old in archives[7:]:
            if old.is_file() and not old.is_symlink():old.unlink()
        return {'archive':str(final),'verified_files':result['verified_files'],'paused_apps':len(paused)}
    finally:
        try:
            result=recover_pending()
            if result['pending']:raise RuntimeError('application recovery pending: '+json.dumps(result))
        finally:
            if temporary and Path(temporary).exists():Path(temporary).unlink()

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('create')
    sub.add_parser('recover')
    sub.add_parser('verify').add_argument('archive')
    target=sub.add_parser('restore');target.add_argument('archive');target.add_argument('--destination',required=True)
    args=parser.parse_args();guard()
    STORE.mkdir(parents=True,exist_ok=True,mode=0o700)
    cancelled=[False]
    def cancel(*args):
        if not cancelled[0]:
            cancelled[0]=True
            raise RuntimeError('backup cancelled; application recovery will run')
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,cancel)
    if args.action=='create':result=create()
    elif args.action=='recover':result=recover()
    elif args.action=='verify':result=verify(args.archive)
    else:result=restore(args.archive,Path(args.destination))
    print(json.dumps(result))
    if result.get('pending'):raise RuntimeError('application recovery pending')

if __name__=='__main__':
    try:main()
    except Exception as exc:print('frl-backup: '+str(exc),file=sys.stderr);sys.exit(1)
