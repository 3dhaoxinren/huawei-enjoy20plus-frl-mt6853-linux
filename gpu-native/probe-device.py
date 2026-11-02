"""Bounded real Mali -> imported DRM DMA-BUF experiment, without changing scanout."""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import mmap
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import uuid

helper_path = Path('/usr/local/lib/frl-gpu-bridge/prepare.py')
assert hashlib.sha256(helper_path.read_bytes()).hexdigest() == '5a7b8e9c31fe7c526c5a91bdd355b6182b17601c6fd59f368c81b525903b9ca4'
spec = importlib.util.spec_from_file_location('gpu_prepare', helper_path)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
helper.BASE = Path('/run/frl/gpu-dmabuf-' + uuid.uuid4().hex)
helper.ROOT = helper.BASE / 'root'
helper.BIN = Path('/tmp/frl-gpu-dmabuf')
helper.BIN_SHA = '6fb31beebb66508384d42df9fe1bc2a29b314c7caa6dbf65ce67a006ba71abcf'
result = {'transport': 'DRM PRIME DMA-BUF import; no per-frame readback', 'scanout_changed': False}
fd = dma = -1
handle = 0
mapping = None

def preparation(action):
    output = io.StringIO()
    sys.argv = ['prepare.py', action]
    with contextlib.redirect_stdout(output):
        helper.main()
    return json.loads(output.getvalue())

try:
    result['prepared'] = preparation('prepare')
    fd = os.open('/dev/dri/card0', os.O_RDWR | os.O_CLOEXEC)
    data = bytearray(struct.pack('IIIIIIQ', 64, 64, 32, 0, 0, 0, 0))
    fcntl.ioctl(fd, 0xc02064b2, data)
    height, width, bpp, flags, handle, pitch, size = struct.unpack('IIIIIIQ', data)
    export = bytearray(struct.pack('IIi', handle, os.O_CLOEXEC | os.O_RDWR, -1))
    fcntl.ioctl(fd, 0xc00c642d, export)
    dma = struct.unpack('IIi', export)[2]
    result['allocation'] = {'width': width, 'height': height, 'pitch': pitch, 'size': size, 'dma_fd': dma}
    cmd = ['/usr/bin/env', '-i', 'PATH=/usr/bin:/bin',
           'LD_LIBRARY_PATH=/apex/com.android.runtime/lib64/bionic:/vendor/lib64:/vendor/lib64/egl:/system/lib64/vndk-sp-29:/system/lib64/vndk-29:/system/lib64',
           '/usr/sbin/chroot', str(helper.ROOT), '/test/frl-gpu-producer', '--import-dmabuf', str(dma), str(pitch), str(width), str(height)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=20, pass_fds=(dma,))
    result['command'] = cmd
    result['exit'] = p.returncode
    result['stdout'] = p.stdout
    result['stderr'] = p.stderr
    if p.returncode == 0:
        data = bytearray(struct.pack('IIQ', handle, 0, 0))
        fcntl.ioctl(fd, 0xc01064b3, data)
        offset = struct.unpack('IIQ', data)[2]
        mapping = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ, offset=offset)
        # Read-only verification of the shared object, not an application rendering path.
        fcntl.ioctl(dma, 0x40086200, struct.pack('Q', 1))
        pixels = [tuple(mapping[y * pitch + x * 4:y * pitch + x * 4 + 4]) for y in range(height) for x in range(width)]
        fcntl.ioctl(dma, 0x40086200, struct.pack('Q', 5))
        from collections import Counter
        result['pixel_counts'] = {str(k): v for k, v in Counter(pixels).items()}
        result['passed'] = len(set(pixels)) == 2 and sorted(Counter(pixels).values()) == [2048, 2048] and all(p[3] == 255 for p in pixels)
    else:
        result['passed'] = False
except Exception as exc:
    result['error'] = repr(exc)
    result['passed'] = False
finally:
    if mapping is not None:
        mapping.close()
    if dma >= 0:
        os.close(dma)
    if handle:
        fcntl.ioctl(fd, 0xc00464b4, struct.pack('I', handle))
    if fd >= 0:
        os.close(fd)
    if helper.ROOT.exists():
        try:
            result['cleanup'] = preparation('cleanup')
        except Exception as exc:
            result['cleanup_error'] = repr(exc)
            result['passed'] = False
print(json.dumps(result))
