"""Single-primary-contact evdev touch; no exclusive grab of the power daemon input."""
import fcntl
import os
import re
from pathlib import Path
import stat
import struct
import time

EVENT = struct.Struct('@llHHi')


class Touch:
    def __init__(self):
        node = next(p for p in Path('/sys/class/input').glob('event*')
                    if (p / 'device/name').read_text().strip() == 'input_mt_wrapper')
        if not re.fullmatch(r'event[0-9]+', node.name):
            raise RuntimeError('invalid kernel event basename')
        self.fd = os.open('/dev/input/' + node.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        st = os.fstat(self.fd)
        major, minor = map(int, (node / 'dev').read_text().split(':'))
        if not stat.S_ISCHR(st.st_mode) or st.st_rdev != os.makedev(major, minor):
            raise RuntimeError('touch device identity differs')
        fcntl.ioctl(self.fd, 0x400445a0, struct.pack('i', 1))
        self.ranges = []
        for axis in (53, 54):
            value = fcntl.ioctl(self.fd, 0x80184540 + axis, bytes(24))
            _, low, high, *_ = struct.unpack('6i', value)
            if high <= low:
                raise RuntimeError('invalid touch range')
            self.ranges.append((low, high))
        bits = int.from_bytes(fcntl.ioctl(self.fd, 0x80084523, bytes(8)), 'little')
        self.type_b = bool(bits & (1 << 47))
        self.slot, self.down, self.start, self.end = 0, False, None, False
        self.contact = 0
        self.x = self.y = 0
        self.partial = b''

    def read(self):
        self.motions = []
        try:
            data = self.partial + os.read(self.fd, EVENT.size * 128)
        except BlockingIOError:
            return []
        if not data:
            raise RuntimeError('touch device disconnected')
        n = len(data) // EVENT.size * EVENT.size
        self.partial = data[n:]
        return self.feed(data[:n])

    def feed(self, data):
        actions = []
        self.motions = []
        for sec, usec, kind, code, value in EVENT.iter_unpack(data):
            if abs(time.monotonic() - sec - usec / 1000000) > 2:
                if self.start is not None:
                    self.motions.append(('cancel',None,None))
                    self.start=None;self.down=False;self.end=False;self.discarding=True
                continue
            if kind == 0 and code == 3:  # SYN_DROPPED: never act on incomplete input.
                self.motions.append(('cancel',None,None))
                self.start = None;self.down = False;self.end=False;self.discarding=True
            elif getattr(self,'discarding',False):
                if kind==0 and code==0:self.discarding=False;self.contact=0
            elif kind == 1 and code == 330:
                if value == 1 and not self.down:
                    self.down = True;self.start = None
                elif value == 0:
                    self.end = True;self.down = False
            elif kind == 0 and code == 2:  # Type-A SYN_MT_REPORT.
                self.contact += 1
            elif kind == 3:
                if code == 47:
                    self.slot = value
                elif self.slot == 0 and (self.type_b or self.contact == 0):
                    if code == 57:
                        if value >= 0 and not self.down:
                            self.down = True;self.start = None
                        elif value < 0:
                            self.end = True;self.down = False
                    elif code in (53, 54):
                        low, high = self.ranges[code - 53]
                        coordinate = (value - low) / (high - low) * (540 if code == 53 else 1200)
                        if code == 53:self.x = coordinate
                        else:self.y = coordinate
            elif kind == 0 and code == 0:
                self.contact = 0
                if self.end:
                    if self.start:
                        actions.append((self.start, (self.x, self.y)))
                        self.motions.append(('up',self.start,(self.x,self.y)))
                    self.end = False;self.start = None
                elif self.down and self.start is None:
                    self.start = (self.x, self.y)
                    self.motions.append(('down',self.start,self.start))
                elif self.down:
                    self.motions.append(('move',self.start,(self.x,self.y)))
        return actions

    def close(self):
        os.close(self.fd)
