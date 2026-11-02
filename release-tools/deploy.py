"""Install a pinned, bounded server bundle and update the complete payload manifest."""
import hashlib,json,os,stat,sys,tarfile,tempfile
from pathlib import Path,PurePosixPath

ROOT=Path('/');MANIFEST=Path('/etc/frl/root-payload.sha256')
assert os.geteuid()==0 and Path('/proc/1/comm').read_text().strip()=='frl-init'
assert Path('/sys/firmware/devicetree/base/compatible').read_bytes()==b'mediatek,MT6853\0'
assert len(sys.argv)==4
archive=Path(sys.argv[1]);digest=sys.argv[2];label=sys.argv[3]
assert archive.parent==Path('/tmp') and label.replace('-','').isalnum()
assert hashlib.sha256(archive.read_bytes()).hexdigest()==digest
old_manifest=MANIFEST.read_bytes()
entries={line.split(None,1)[1]:line.split(None,1)[0] for line in old_manifest.decode().splitlines()}
for name,want in entries.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==want,name
with tarfile.open(archive) as tar:
    members=tar.getmembers();meta=json.load(tar.extractfile('bundle.json'))
    assert len(members)==len(meta['files'])+1
    files={}
    for member in members:
        if member.name=='bundle.json':continue
        name=member.name;parts=PurePosixPath(name).parts
        assert member.isfile() and not member.name.startswith('/') and '..' not in parts
        assert name in meta['files'] and name not in files
        assert name.startswith(('usr/lib/frl/','usr/local/sbin/','etc/frl/','etc/sudoers.d/')) or name=='etc/hosts'
        info=meta['files'][name];data=tar.extractfile(member).read()
        assert hashlib.sha256(data).hexdigest()==info['sha256']
        assert info['mode'] in (0o600,0o644,0o755,0o440)
        if (ROOT/name).exists():assert not (ROOT/name).is_symlink(),name
        files[name]=(data,info['mode'],info['mutable'])
backup=Path('/var/backups/frl')/label;backup.mkdir(mode=0o700,exist_ok=False)

def syncdir(p):
    fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def atomic(path,data,mode):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.frl-deploy-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as f:
            os.fchmod(f.fileno(),mode);f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temp,path);syncdir(path.parent)
    finally:
        if os.path.exists(temp):os.unlink(temp)

old={}
for name in files:
    path=ROOT/name
    if path.exists():
        old[name]=(path.read_bytes(),stat.S_IMODE(path.stat().st_mode))
        atomic(backup/name,*old[name])
atomic(backup/'root-payload.sha256',old_manifest,0o600)
written=[]
try:
    for name,(data,mode,mutable) in files.items():
        written.append(name);atomic(ROOT/name,data,mode)
        if not mutable:entries[name]=hashlib.sha256(data).hexdigest()
        else:assert name not in entries,'tracked code cannot become mutable'
    atomic(MANIFEST,(''.join(d+'  '+n+'\n' for n,d in entries.items())).encode(),0o644)
    for name,want in entries.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==want,name
    result={'installed':written,'new_files':[n for n in written if n not in old],
            'backup':str(backup),'payload_checks':len(entries)}
    atomic(backup/'receipt.json',json.dumps(result,indent=2).encode(),0o600)
    print(json.dumps(result))
except BaseException:
    for name in reversed(written):
        if name in old:atomic(ROOT/name,*old[name])
        elif (ROOT/name).exists():(ROOT/name).unlink();syncdir((ROOT/name).parent)
    atomic(MANIFEST,old_manifest,0o644)
    raise
