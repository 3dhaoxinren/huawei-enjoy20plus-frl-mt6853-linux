"""Build a reproducible tar bundle for the guarded native installer."""
import hashlib,io,json,tarfile
from pathlib import Path

def build(path,files):
    meta={'files':{name:{'sha256':hashlib.sha256(data).hexdigest(),'mode':mode,'mutable':mutable}
                   for name,(data,mode,mutable) in files.items()}}
    with tarfile.open(path,'w:gz',format=tarfile.USTAR_FORMAT) as tar:
        for name,data,mode in [('bundle.json',json.dumps(meta).encode(),0o600)]+[(n,d,m) for n,(d,m,_) in files.items()]:
            info=tarfile.TarInfo(name);info.size=len(data);info.mode=mode;info.mtime=0
            tar.addfile(info,io.BytesIO(data))
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
