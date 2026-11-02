"""Bounded GPU worker lifecycle. Software rendering is deliberately unavailable."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import time
import uuid
from kms import Display

MAGIC = 0x474c5246
HELPER_SHA = '5a7b8e9c31fe7c526c5a91bdd355b6182b17601c6fd59f368c81b525903b9ca4'
RENDERER_SHA = '63846a8bca2910105440f25db0f2bef79468763900b9118555b7c064f72bfc58'


class Engine:
    def __init__(self, binary, log_path):
        self.worker = self.display = self.log = self.helper = None
        self.frames, self.index = 0, 0
        self.draw_ms = 0
        try:
            path = Path('/usr/local/lib/frl-gpu-bridge/prepare.py')
            assert hashlib.sha256(path.read_bytes()).hexdigest() == HELPER_SHA
            spec = importlib.util.spec_from_file_location('gpu_prepare', path)
            self.helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.helper)
            self.helper.BASE = Path('/run/frl/gpu-monitor-' + uuid.uuid4().hex)
            self.helper.ROOT = self.helper.BASE / 'root'
            self.helper.BIN = Path(binary)
            self.helper.BIN_SHA = RENDERER_SHA
            self.prepare('prepare')
            self.display = Display()
            buffers = [self.display.allocate() for _ in range(2)]
            cmd = ['/usr/bin/env', '-i', 'PATH=/usr/bin:/bin',
                   'LD_LIBRARY_PATH=/apex/com.android.runtime/lib64/bionic:/vendor/lib64:/vendor/lib64/egl:/system/lib64/vndk-sp-29:/system/lib64/vndk-29:/system/lib64',
                   '/usr/sbin/chroot', str(self.helper.ROOT), '/test/frl-gpu-producer', '--scene',
                   *(str(b['dma']) for b in buffers), str(self.display.pitch), '1080', '2400']
            self.log = open(log_path, 'wb')
            self.worker = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=self.log, pass_fds=tuple(b['dma'] for b in buffers), bufsize=0)
            self.ack(0xffffffff)
        except BaseException:
            self.close()
            raise

    def prepare(self, action):
        output, previous = io.StringIO(), sys.argv
        try:
            sys.argv = ['prepare.py', action]
            with contextlib.redirect_stdout(output):
                self.helper.main()
            return json.loads(output.getvalue())
        finally:
            sys.argv = previous

    def ack(self, expected):
        data, deadline = b'', time.monotonic() + 5
        while len(data) < 8:
            if not select.select([self.worker.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                raise TimeoutError('Mali renderer deadline; refusing CPU fallback')
            part = os.read(self.worker.stdout.fileno(), 8 - len(data))
            if not part:
                raise RuntimeError('Mali renderer exited')
            data += part
        if struct.unpack('II', data) != (MAGIC, expected):
            raise RuntimeError('invalid GPU completion acknowledgement')

    def send(self, data):
        view, deadline = memoryview(data), time.monotonic() + 5
        fd = self.worker.stdin.fileno()
        # Bounded writes avoid a stuck GPU helper holding the controller forever.
        os.set_blocking(fd, False)
        while view:
            if not select.select([], [fd], [], max(0, deadline - time.monotonic()))[1]:
                raise TimeoutError('GPU command pipe stalled')
            try:
                n = os.write(fd, view[:65536])
                view = view[n:]
            except BlockingIOError:
                pass

    def draw(self, canvas):
        start = time.monotonic()
        count = len(canvas.vertices) // 8
        if count > 120000:
            raise RuntimeError('scene exceeds bounded vertex capacity')
        packets = []
        for x, y, w, h, data in canvas.atlas.updates:
            packets.append(struct.pack('6I', MAGIC, 1, x, y, w, h) + data)
        canvas.atlas.updates.clear()
        packets.append(struct.pack('6I', MAGIC, 2, self.index, count, 0, 0) + canvas.vertices.tobytes())
        self.send(b''.join(packets))
        self.ack(self.index)
        self.display.present(self.index)
        self.index = 1 - self.index
        self.frames += 1
        self.draw_ms = (time.monotonic() - start) * 1000

    def close(self):
        errors = []
        if self.worker:
            self.worker.stdin.close()
            try:
                self.worker.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.worker.terminate()
                try:
                    self.worker.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.worker.kill();self.worker.wait(timeout=2)
            self.worker.stdout.close()
            self.worker = None
        if self.display:
            try:
                self.display.close()
            except Exception as exc:
                errors.append(str(exc))
            self.display = None
        if self.log:
            self.log.close();self.log = None
        if self.helper and self.helper.ROOT.exists():
            try:
                self.prepare('cleanup')
            except Exception as exc:
                errors.append(str(exc))
            self.helper = None
        if errors:
            raise RuntimeError('GPU cleanup: ' + '; '.join(errors))
