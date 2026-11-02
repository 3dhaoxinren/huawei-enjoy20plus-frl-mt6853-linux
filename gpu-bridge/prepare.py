#!/usr/bin/python3
"""Prepare/clean only a new RAM GPU test root. Never executes vendor code."""
import fcntl,hashlib,json,os,re,stat,struct,subprocess,sys
from pathlib import Path
sys.dont_write_bytecode=True
BASE=Path('/run/frl/gpu-wayland-bridge-v1')
ROOT=BASE/'root'
BIN_SHA='c7f5d47236a4fe549a2d3c0f7ac3512bf6a3937f61aae41ee0f044469e243208'
INIT_SHA='2c198d27cd368940a548368a11405a55edd582cc2ae58e8ebcfbdf5614f447f4'
BIN=Path('/tmp/frl-gpu-producer')
APEX=Path('/run/frl/fuel/apex')
APEX_IMAGE=Path('/run/frl/fuel/apex_payload.img')
APEX_SHA='98d0f48fb715bb81b11e470086611179c522134dd673f69d1a4d91e5382c2cd1'
APEX_SIZE=107462656
MOUNTS=[('/run/frl/stock/vendor','vendor'),('/run/frl/stock/system/system','system'),
        ('/run/frl/fuel/apex','apex/com.android.runtime'),('/proc','proc'),('/sys','sys')]

def need(ok,msg):
    if not ok:raise RuntimeError(msg)

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def rows():
    out=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        a,b=line.split(' - ',1);a=a.split();b=b.split()
        out.append({'target':a[4],'type':b[0],'options':a[5].split(','),'id':int(a[0]),'dev':a[2],
                    'root':a[3],'source':b[1],'super_options':b[2].split(',')})
    return out

def cover(p):
    matches=[r for r in rows() if str(p)==r['target'] or str(p).startswith(r['target'].rstrip('/')+'/')]
    length=max(len(r['target']) for r in matches);found=[r for r in matches if len(r['target'])==length]
    need(len(found)==1,'stacked mounts refused');return found[0]

def command(*args):subprocess.run(args,check=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,timeout=15)

def safe_dir(p):
    s=p.lstat();need(stat.S_ISDIR(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022,'unsafe directory: '+str(p))

def verified_apex():
    # This exception is ONLY the pinned production APEX root, not safe_dir().
    for p in (Path('/run'),Path('/run/frl'),APEX.parent):safe_dir(p)
    image_mount=cover(APEX_IMAGE)
    need(image_mount['type']=='tmpfs','APEX shadow image not RAM')
    s=APEX.lstat()
    need(stat.S_ISDIR(s.st_mode) and (s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode))==(1000,1000,0o755),'production APEX root metadata differs')
    row=cover(APEX)
    need(row['target']==str(APEX) and row['root']=='/' and row['type']=='ext4'
         and {'ro','nosuid','nodev'}<=set(row['options']) and 'ro' in row['super_options']
         and {'norecovery','noload'}&set(row['super_options']),'APEX must be exact readonly ext4 noload mount')
    loop=(Path('/sys/dev/block')/row['dev']).resolve(strict=True)
    need(loop.is_relative_to('/sys/devices') and re.fullmatch(r'loop[0-9]+',loop.name)
         and (loop/'dev').read_text().strip()==row['dev'],'APEX loop sysfs identity differs')
    need((loop/'loop/backing_file').read_text().strip()==str(APEX_IMAGE),'APEX loop backing path differs')
    need((loop/'ro').read_text().strip()=='1' and (loop/'size').read_text().strip()==str(APEX_SIZE//512),'APEX loop RO/size differs')
    safe_dir(Path('/dev'));safe_dir(Path('/dev/frl-fuel'))
    node=Path('/dev/frl-fuel')/loop.name;n=node.lstat()
    need(stat.S_ISBLK(n.st_mode) and n.st_rdev==os.makedev(*map(int,row['dev'].split(':')))
         and n.st_uid==n.st_gid==0 and stat.S_IMODE(n.st_mode)==0o600,'private APEX loop node differs')
    imagefd=os.open(APEX_IMAGE,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        before=os.fstat(imagefd)
        need(stat.S_ISREG(before.st_mode) and before.st_uid==before.st_gid==0 and before.st_nlink==1
             and not before.st_mode&0o022 and before.st_size==APEX_SIZE,'unsafe APEX shadow image')
        h=hashlib.sha256();remaining=APEX_SIZE
        while remaining:
            chunk=os.read(imagefd,min(1024*1024,remaining));need(chunk,'short APEX image read')
            h.update(chunk);remaining-=len(chunk)
        after=os.fstat(imagefd)
        need(h.hexdigest()==APEX_SHA and (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==
             (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),'APEX payload hash/identity changed')
        fd=os.open(node,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
        try:
            need(os.path.samestat(n,os.fstat(fd)),'APEX loop node changed')
            fmt='=QQQQQIIII64s64s32sQQ'
            info=struct.unpack(fmt,fcntl.ioctl(fd,0x4C05,bytes(struct.calcsize(fmt))))
            need(info[:3]==(before.st_dev,before.st_ino,before.st_rdev) and info[3:5]==(0,0)
                 and info[8]&5==5,'APEX loop backing inode/offset/RO/autoclear differs')
            need(struct.unpack('=i',fcntl.ioctl(fd,0x125E,bytes(4)))[0]==1
                 and struct.unpack('=Q',fcntl.ioctl(fd,0x80081272,bytes(8)))[0]==APEX_SIZE,'APEX block RO/size differs')
        finally:os.close(fd)
        need(os.path.samestat(before,APEX_IMAGE.lstat()) and cover(APEX)==row,'APEX path/mount changed')
        return {'mount_id':row['id'],'dev':row['dev'],'loop':loop.name,'payload_sha256':APEX_SHA,
                'payload_bytes':APEX_SIZE,'image_identity':[before.st_dev,before.st_ino]}
    finally:os.close(imagefd)

def metadata_node(name,source):
    s=source.lstat();need(stat.S_ISCHR(s.st_mode) and s.st_uid==0,'source not root character node: '+str(source))
    major,minor=os.major(s.st_rdev),os.minor(s.st_rdev)
    fields=dict(line.split('=',1) for line in Path(f'/sys/dev/char/{major}:{minor}/uevent').read_text().splitlines() if '=' in line)
    need(fields.get('DEVNAME')==name and fields.get('MAJOR')==str(major) and fields.get('MINOR')==str(minor),'node/sysfs disagreement')
    return s.st_rdev

def prepare():
    need(not BASE.exists() and not BASE.is_symlink(),'GPU RAM directory exists; no blind replay')
    for p in (Path('/run'),Path('/run/frl')):safe_dir(p)
    need(cover(BASE)['type']=='tmpfs' and 'ro' not in cover(BASE)['options'],'GPU root must be writable RAM tmpfs')
    s=BIN.lstat();need(stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_nlink==1 and not s.st_mode&0o022 and sha(BIN)==BIN_SHA,'binary pin/metadata differs')
    need(sha(Path('/run/frl/stock/vendor/lib64/egl/libGLES_mali.so'))=='618a2dd8c527e0424b253e97516d3c95b96f6abe06e8498a802b4ebf43d59c19','Mali pin differs')
    apex_evidence=verified_apex()
    for source,target in MOUNTS:
        if Path(source)!=APEX:safe_dir(Path(source))
        need('ro' in cover(Path(source))['options'] or source in ('/proc','/sys'),'stock/APEX source not readonly')
    need(Path('/run/frl/fuel/apex/bin/linker64').is_file() and Path('/run/frl/fuel/apex/lib64/bionic/libc.so').is_file(),'full ARM64 APEX absent')
    nodes={name:metadata_node(name,Path('/dev')/name) for name in ('null','urandom','mali0','ion')}
    BASE.mkdir(mode=0o700);ROOT.mkdir(mode=0o700)
    for name in ('dev','proc','sys','vendor','system','apex/com.android.runtime','tmp','data/local/tmp','data/gpu','data/vendor/gpu','test'):
        (ROOT/name).mkdir(parents=True,exist_ok=True,mode=0o700)
    # Copy only the pinned test binary to RAM. No vendor assets or calibration copied.
    fd=os.open(ROOT/'test/frl-gpu-producer',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o700)
    with os.fdopen(fd,'wb') as f:f.write(BIN.read_bytes());f.flush();os.fsync(f.fileno())
    need(sha(ROOT/'test/frl-gpu-producer')==BIN_SHA,'RAM executable readback mismatch')
    mounted=[]
    try:
        # Native /run is nodev. A separate tiny RAM /dev enables only four nodes,
        # without changing /run flags or binding the host's full device tree.
        command('/usr/bin/mount','-t','tmpfs','-o','rw,nosuid,noexec,mode=0700,size=1m','tmpfs',str(ROOT/'dev'))
        mounted.append(str(ROOT/'dev'))
        d=cover(ROOT/'dev');need(d['target']==str(ROOT/'dev') and d['type']=='tmpfs' and not {'ro','nodev'}&set(d['options']),'private device tmpfs flags differ')
        for name,dev in nodes.items():os.mknod(ROOT/'dev'/name,stat.S_IFCHR|0o600,dev)
        for source,target in MOUNTS:
            dst=ROOT/target;command('/usr/bin/mount','--bind',source,str(dst));mounted.append(str(dst))
            command('/usr/bin/mount','-o','remount,bind,ro,nosuid,nodev',str(dst))
            r=cover(dst);need(r['target']==str(dst) and 'ro' in r['options'] and os.path.samefile(source,dst),'bind verification failed')
            if Path(source)==APEX:need(verified_apex()==apex_evidence,'production APEX changed during bind')
        print(json.dumps({'schema':'FRL_GPU_RAM_ROOT_1','prepared':True,'vendor_executed':False,
          'root':str(ROOT),'mounts':mounted,'binary_sha256':BIN_SHA,'apex_evidence':apex_evidence,
          'missing_nodes_are_not_fabricated':['binder','hwbinder','vndbinder','ged','DRM'],
          'command':['/usr/bin/env','-i','PATH=/usr/bin:/bin',
            'LD_LIBRARY_PATH=/apex/com.android.runtime/lib64/bionic:/vendor/lib64:/vendor/lib64/egl:/system/lib64/vndk-sp-29:/system/lib64/vndk-29:/system/lib64',
            '/usr/sbin/chroot',str(ROOT),'/test/frl-gpu-producer','--render-stream']}))
    except BaseException:
        print(json.dumps({'prepared':False,'created_mounts':mounted,'root':str(ROOT),'cleanup':'ordinary exact umount only; do not force/lazy detach'}),file=sys.stderr)
        raise

def cleanup():
    safe_dir(BASE);safe_dir(ROOT)
    need(cover(BASE)['type']=='tmpfs','GPU root no longer RAM')
    expected={str(ROOT/t):s for s,t in MOUNTS}
    present=[r for r in rows() if r['target'].startswith(str(BASE)+'/') or r['target']==str(BASE)]
    need(len({r['target'] for r in present})==len(present),'stacked GPU mounts')
    for r in present:
        if r['target']==str(ROOT/'dev'):
            need(r['type']=='tmpfs','private device mount changed')
        else:need(r['target'] in expected and os.path.samefile(r['target'],expected[r['target']]),'foreign mount in GPU root')
    for source,target in reversed(MOUNTS):
        dst=ROOT/target
        if any(r['target']==str(dst) for r in present):command('/usr/bin/umount',str(dst))
    if any(r['target']==str(ROOT/'dev') for r in present):command('/usr/bin/umount',str(ROOT/'dev'))
    print(json.dumps({'unmounted':True,'ram_directory_preserved':str(BASE),'files_deleted':False}))

def main():
    need(os.geteuid()==os.getegid()==0 and len(sys.argv)==2 and sys.argv[1] in ('prepare','cleanup'),'native root and prepare/cleanup required')
    need(os.path.samefile('/','/proc/1/root') and sha(Path('/proc/1/exe'))==INIT_SHA,'not actual native root')
    need(Path('/sys/firmware/devicetree/base/compatible').read_bytes()==b'mediatek,MT6853\0','wrong board')
    os.umask(0o077)
    (prepare if sys.argv[1]=='prepare' else cleanup)()

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as e:
        print('FRL_GPU_PREPARE_REFUSED: '+str(e),file=sys.stderr);sys.exit(78)
