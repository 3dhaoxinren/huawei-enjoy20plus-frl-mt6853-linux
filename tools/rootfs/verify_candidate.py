#!/usr/bin/env python3
"""Read-only offline check, works on Windows/Linux. Does not flash or extract."""
import gzip
import hashlib
import json
from pathlib import Path
import re
import struct
import sys


def verify(folder):
    folder=Path(folder)
    report=json.loads((folder/'BUILD-REPORT.json').read_text(encoding='utf-8'))
    fields=(folder/'v15-deployed.flag').read_text(encoding='ascii').splitlines()
    if (len(fields)!=5 or fields[:2]!=['FRL_V15_IMAGE_V2','rootfs-v3.ext4']
            or fields[4]!='staged' or not re.fullmatch('[0-9a-f]{32}',fields[2])
            or not re.fullmatch('[0-9a-f]{64}',fields[3])):
        raise ValueError('invalid staged flag')
    for name in ('rootfs-v3.ext4.gz','v15-deployed.flag'):
        h=hashlib.sha256()
        with (folder/name).open('rb') as f:
            for b in iter(lambda:f.read(1048576),b''):h.update(b)
        if h.hexdigest()!=report['artifact_sha256'][name]:
            raise ValueError('artifact SHA256 mismatch: '+name)
    h=hashlib.sha256();size=0;head=b''
    with gzip.open(folder/'rootfs-v3.ext4.gz','rb') as f:
        for b in iter(lambda:f.read(1048576),b''):
            h.update(b);size+=len(b)
            if not head:head=b[:2048]
    if size!=4294967296 or h.hexdigest()!=fields[3] or h.hexdigest()!=report['raw_image_sha256']:
        raise ValueError('raw image size/SHA256 mismatch')
    sb=head[1024:2048]
    u16=lambda n:struct.unpack_from('<H',sb,n)[0]
    u32=lambda n:struct.unpack_from('<I',sb,n)[0]
    if u16(56)!=0xef53 or u32(24)!=2 or u32(4)!=1048576 or u32(336):
        raise ValueError('ext4 geometry mismatch')
    compat,incompat,ro=u32(92),u32(96),u32(100)
    if not compat&4 or incompat&~0x246 or ro&~0x46b or compat&0x1000:
        raise ValueError('unsupported ext4 feature')
    if not u16(58)&1 or u16(58)&2 or incompat&4 or sb[104:120].hex()!=fields[2]:
        raise ValueError('unclean filesystem / wrong UUID')
    return {'passed':True,'bytes':size,'raw_sha256':h.hexdigest(),'uuid':fields[2],
            'does_not_prove_device_boot':True}


if __name__=='__main__':
    print(json.dumps(verify(sys.argv[1] if len(sys.argv)>1 else Path(__file__).resolve().parent.parent),indent=2))
