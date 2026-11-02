"""Bounded, hardware-only full-size scanout and page-flip qualification."""
import contextlib, hashlib, importlib.util, io, json, subprocess, sys, time, uuid
from pathlib import Path
spec=importlib.util.spec_from_file_location('kms','/tmp/frl-kms.py')
kms=importlib.util.module_from_spec(spec);spec.loader.exec_module(kms)
p=Path('/usr/local/lib/frl-gpu-bridge/prepare.py')
assert hashlib.sha256(p.read_bytes()).hexdigest()=='5a7b8e9c31fe7c526c5a91bdd355b6182b17601c6fd59f368c81b525903b9ca4'
spec=importlib.util.spec_from_file_location('prep',p)
prep=importlib.util.module_from_spec(spec);spec.loader.exec_module(prep)
prep.BASE=Path('/run/frl/gpu-scanout-'+uuid.uuid4().hex);prep.ROOT=prep.BASE/'root'
prep.BIN=Path('/tmp/frl-gpu-dmabuf');prep.BIN_SHA='6fb31beebb66508384d42df9fe1bc2a29b314c7caa6dbf65ce67a006ba71abcf'
def preparation(action):
    out=io.StringIO();sys.argv=['prepare.py',action]
    with contextlib.redirect_stdout(out):prep.main()
    return json.loads(out.getvalue())
light=Path('/sys/class/leds/lcd-backlight/brightness')
assert int(light.read_text())==0,'screen must already be off'
d=None;r={}
try:
    r['prepare']=preparation('prepare');d=kms.Display()
    for i in range(2):
        b=d.allocate()
        cmd=['/usr/bin/env','-i','PATH=/usr/bin:/bin','LD_LIBRARY_PATH=/apex/com.android.runtime/lib64/bionic:/vendor/lib64:/vendor/lib64/egl:/system/lib64/vndk-sp-29:/system/lib64/vndk-29:/system/lib64','/usr/sbin/chroot',str(prep.ROOT),'/test/frl-gpu-producer','--import-dmabuf',str(b['dma']),str(d.pitch),str(d.width),str(d.height)]
        p=subprocess.run(cmd,pass_fds=(b['dma'],),capture_output=True,text=True,timeout=20)
        r['render'+str(i)]={'exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
        assert p.returncode==0
    d.present(0)
    Path('/sys/class/graphics/fb0/blank').write_text('0\n');time.sleep(1)
    light.write_text('800\n');time.sleep(.5)
    r['flips']=[]
    for i in range(6):
        start=time.monotonic();d.present((i+1)%2);r['flips'].append(time.monotonic()-start);time.sleep(.1)
    d.screenshot('/tmp/frl-gpu-scanout.png')
    r['pass']=True;r['brightness']=light.read_text();r['buffers']=d.buffers.copy()
except Exception as e:r['error']=repr(e);r['pass']=False
finally:
    light.write_text('0\n')
    if d:d.close()
    r['cleanup']=preparation('cleanup')
print(json.dumps(r))
