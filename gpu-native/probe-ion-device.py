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
fd = dma = ionfd = -1
ionhandle = 0
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
    ionfd = os.open('/dev/ion', os.O_RDWR | os.O_CLOEXEC)
    height = width = 64
    pitch = width * 4
    size = pitch * height
    data = bytearray(struct.pack('QQIIi4x', size, 4096, 1 << 10, 0, 0))
    fcntl.ioctl(ionfd, 0xc0204900, data)
    ionhandle = struct.unpack('QQIIi4x', data)[4]
    export = bytearray(struct.pack('ii', ionhandle, -1))
    fcntl.ioctl(ionfd, 0xc0084904, export)
    dma = struct.unpack('ii', export)[1]
    fd = os.open('/dev/dri/card0', os.O_RDWR | os.O_CLOEXEC)
    imported = bytearray(struct.pack('IIi', 0, 0, dma))
    fcntl.ioctl(fd, 0xc00c642e, imported)
    handle = struct.unpack('IIi', imported)[0]
    result['ion_to_drm'] = {'ion_handle': ionhandle, 'drm_handle': handle, 'heap': 10}
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
        mapping = mmap.mmap(dma, size, mmap.MAP_SHARED, mmap.PROT_READ)
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
        fcntl.ioctl(fd, 0x40086409, struct.pack('II', handle, 0))
    if fd >= 0:
        os.close(fd)
    if ionhandle:
        fcntl.ioctl(ionfd, 0xc0044901, struct.pack('i', ionhandle))
    if ionfd >= 0:
        os.close(ionfd)
    if helper.ROOT.exists():
        try:
            result['cleanup'] = preparation('cleanup')
        except Exception as exc:
            result['cleanup_error'] = repr(exc)
            result['passed'] = False
print(json.dumps(result))
