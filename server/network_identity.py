"""Apply the device's persistent station MAC before WPA or DHCP can start."""
import os,re,stat,subprocess
from pathlib import Path

CONFIG=Path('/etc/frl/network/wlan-mac')
ADDRESS=Path('/sys/class/net/wlan0/address')


def read_identity():
    fd=os.open(CONFIG,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise RuntimeError('untrusted station MAC configuration')
        mac=os.read(fd,64).decode().strip()
        if not re.fullmatch(r'02(:[0-9a-f]{2}){5}',mac):raise ValueError('expected a locally administered unicast MAC')
        return mac
    finally:os.close(fd)


def apply_identity():
    mac=read_identity();before=ADDRESS.read_text().strip()
    if before!=mac:
        for args in [('down',),('address',mac)]:
            subprocess.run(['/usr/sbin/ip','link','set','dev','wlan0',*args],check=True,timeout=5,capture_output=True)
        if ADDRESS.read_text().strip()!=mac:raise RuntimeError('station MAC readback differs')
    return {'previous':before,'configured':mac,'verified':True}
