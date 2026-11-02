"""MT6853 ION -> DRM PRIME scanout; no software renderer or pixel-copy presentation."""
import ctypes
import fcntl
import mmap
import os
import select
import struct
import time


def ioctl(fd, request, fmt, *values):
    data = bytearray(struct.pack(fmt, *values))
    fcntl.ioctl(fd, request, data)
    return struct.unpack(fmt, data)


class Display:
    def __init__(self):
        self.fd = os.open('/dev/dri/card0', os.O_RDWR | os.O_CLOEXEC)
        self.ion = -1
        self.buffers = []
        self.active = False
        self.crtc = None
        self.saved = None
        try:
            fcntl.ioctl(self.fd, 0x641e)  # DRM_IOCTL_SET_MASTER; fails if compositor owns it.
            res = ioctl(self.fd, 0xc04064a0, '4Q8I', *([0] * 12))
            counts = res[4:8]
            assert all(0 <= n <= 128 for n in counts), 'invalid DRM resource counts'
            arrays = [(ctypes.c_uint32 * max(1, n))() for n in counts]
            res = ioctl(self.fd, 0xc04064a0, '4Q8I', *(list(map(ctypes.addressof, arrays)) + list(counts) + [0] * 4))
            crtcs = list(arrays[1])[:res[5]]
            self.connector = None
            for connector in list(arrays[2])[:res[6]]:
                args = [0] * 16
                args[8] = connector
                con = ioctl(self.fd, 0xc05064a7, '4Q12I', *args)
                if con[9] != 16 or con[11] != 1 or not con[4]:
                    continue  # Physical DSI only; never choose writeback outputs.
                assert con[4] <= 64 and con[6] <= 16
                modes = (ctypes.c_ubyte * (68 * con[4]))()
                encoders = (ctypes.c_uint32 * max(1, con[6]))()
                args = list(con)
                args[:4] = [ctypes.addressof(encoders), ctypes.addressof(modes), 0, 0]
                args[5] = 0
                con = ioctl(self.fd, 0xc05064a7, '4Q12I', *args)
                candidates = [bytes(modes[i * 68:(i + 1) * 68]) for i in range(con[4])]
                self.mode = next(m for m in candidates if struct.unpack_from('H', m, 4)[0] == 1080
                                 and struct.unpack_from('H', m, 14)[0] == 2400
                                 and struct.unpack_from('I', m, 24)[0] == 60)
                encoder = ioctl(self.fd, 0xc01464a6, '5I', encoders[0], 0, 0, 0, 0)
                self.crtc = next(c for i, c in enumerate(crtcs) if encoder[3] & (1 << i))
                self.connector = connector
                break
            if self.connector is None:
                raise RuntimeError('no qualified 1080x2400@60 DSI connector')
            self.width, self.height = 1080, 2400
            self.pitch = (self.width * 4 + 63) & ~63
            saved = bytearray(struct.pack('Q7I68s', 0, 0, self.crtc, 0, 0, 0, 0, 0, bytes(68)))
            fcntl.ioctl(self.fd, 0xc06864a1, saved)
            self.saved = bytes(saved)
            # We only take over a disabled CRTC; never steal another running display.
            assert struct.unpack('Q7I68s', self.saved)[3] == 0, 'active framebuffer exists'
            self.ion = os.open('/dev/ion', os.O_RDWR | os.O_CLOEXEC)
        except BaseException:
            self.close()
            raise

    def allocate(self):
        item = {'ion_handle': 0, 'dma': -1, 'gem': 0, 'fb': 0}
        self.buffers.append(item)
        size = self.pitch * self.height
        value = ioctl(self.ion, 0xc0204900, 'QQIIi4x', size, 4096, 1 << 10, 0, 0)
        item['ion_handle'] = value[4]
        item['dma'] = ioctl(self.ion, 0xc0084904, 'ii', item['ion_handle'], -1)[1]
        item['gem'] = ioctl(self.fd, 0xc00c642e, 'IIi', 0, 0, item['dma'])[0]
        # ARGB8888 is shared with EGL, alpha always opaque for the output surface.
        result = ioctl(self.fd, 0xc06864b8, '5I4I4I4I4x4Q', 0, self.width, self.height, 0x34325241, 0,
                       item['gem'], 0, 0, 0, self.pitch, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        item['fb'] = result[0]
        item['size'] = size
        return item

    def present(self, index):
        item = self.buffers[index]
        if not self.active:
            connector = (ctypes.c_uint32 * 1)(self.connector)
            data = bytearray(struct.pack('Q7I68s', ctypes.addressof(connector), 1, self.crtc,
                                        item['fb'], 0, 0, 0, 1, self.mode))
            fcntl.ioctl(self.fd, 0xc06864a2, data)
            self.active = True
        else:
            ioctl(self.fd, 0xc01864b0, '4IQ', self.crtc, item['fb'], 1, 0, index + 1)
            deadline = time.monotonic() + 2
            while True:
                if not select.select([self.fd], [], [], max(0, deadline - time.monotonic()))[0]:
                    raise TimeoutError('DRM page flip did not complete')
                data = os.read(self.fd, 4096)
                offset = 0
                found = False
                while offset + 8 <= len(data):
                    kind, length = struct.unpack_from('II', data, offset)
                    if length < 8 or offset + length > len(data):
                        raise RuntimeError('invalid DRM event')
                    if kind == 2:
                        found = True
                    offset += length
                if found:
                    break
        self.current = index

    def screenshot(self, path):
        """Explicit diagnostic only: read the currently scanned-out GPU buffer once."""
        import cairo
        item = self.buffers[self.current]
        with mmap.mmap(item['dma'], item['size'], mmap.MAP_SHARED, mmap.PROT_READ) as data:
            fcntl.ioctl(item['dma'], 0x40086200, struct.pack('Q', 1))
            raw = bytearray(data)
            fcntl.ioctl(item['dma'], 0x40086200, struct.pack('Q', 5))
        surface = cairo.ImageSurface.create_for_data(raw, cairo.FORMAT_ARGB32, self.width, self.height, self.pitch)
        surface.write_to_png(str(path))

    def close(self):
        if self.fd < 0:
            return
        if self.active:
            data = bytearray(struct.pack('Q7I68s', 0, 0, self.crtc, 0, 0, 0, 0, 0, bytes(68)))
            fcntl.ioctl(self.fd, 0xc06864a2, data)
            self.active = False
        for item in reversed(self.buffers):
            if item['fb']:
                ioctl(self.fd, 0xc00464af, 'I', item['fb'])
            if item['gem']:
                ioctl(self.fd, 0x40086409, 'II', item['gem'], 0)
            if item['dma'] >= 0:
                os.close(item['dma'])
            if item['ion_handle']:
                ioctl(self.ion, 0xc0044901, 'i', item['ion_handle'])
        self.buffers.clear()
        if self.ion >= 0:
            os.close(self.ion)
            self.ion = -1
        try:
            fcntl.ioctl(self.fd, 0x641f)
        finally:
            os.close(self.fd)
            self.fd = -1
