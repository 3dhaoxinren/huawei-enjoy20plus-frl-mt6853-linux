#!/usr/bin/python3
"""Observe the native session; feed recovery only after standalone qualification."""
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
import vendor_boot
from server_common import runtime_mode, ssh_listening


def enabled_token(path, token):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        entry = os.fstat(fd)
        if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != 0 or
                entry.st_nlink != 1 or stat.S_IMODE(entry.st_mode) != 0o600):
            return False
        return os.read(fd, 128) == token
    finally:
        os.close(fd)


def wayland_sync(endpoint):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(endpoint)
        # wl_display.sync(new wl_callback id=2), native little-endian wire format.
        client.sendall(struct.pack('<III', 1, 12 << 16, 2))
        data = bytearray()
        while len(data) < 12:
            block = client.recv(4096)
            if not block:
                raise RuntimeError('Wayland display disconnected before sync')
            data.extend(block)
        object_id, header, _serial = struct.unpack_from('<III', data)
        if object_id != 2 or (header & 0xffff) != 0 or (header >> 16) != 12:
            raise RuntimeError('unexpected Wayland sync response')


def touch_alive():
    pid, expected_start = Path('/run/frl/touch/aptouch.pid').read_text().split()
    if not pid.isdecimal() or not expected_start.isdecimal():
        return False
    process = Path('/proc') / pid
    actual_start = (process / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    cmdline = (process / 'cmdline').read_bytes().split(b'\0')
    if actual_start != expected_start or b'/vendor/bin/aptouch_daemon' not in cmdline:
        return False
    targets = []
    for fd in (process / 'fd').iterdir():
        try:
            targets.append(os.readlink(fd))
        except FileNotFoundError:
            # Unrelated transient descriptors can close during a procfs walk.
            continue
    if (process / 'stat').read_text().rsplit(')', 1)[1].split()[19] != expected_start:
        return False
    return (any(path.endswith('/dev/thp') for path in targets) and
            any(path.endswith('/dev/input_mt_wrapper') for path in targets) and
            Path('/dev/frl-props/odm.thp.dae_alive').read_text() == 'true')


def check():
    result = {'monotonic': time.monotonic(), 'pid1': Path('/proc/1/comm').read_text().strip()}
    command = Path('/proc/1/cmdline').read_bytes().split(b'\0')
    if ((b'/sbin/frl-init' not in command[:2] and b'frl-init' not in command[:2]) or
            not os.path.samefile('/proc/1/exe', '/sbin/frl-init') or
            not os.path.samefile('/', '/proc/1/root')):
        raise RuntimeError('PID1/root identity mismatch')
    mounts = [line.split() for line in Path('/proc/mounts').read_text().splitlines()]
    result['root_rw'] = any(m[1] == '/' and m[2] == 'ext4' and 'rw' in m[3].split(',') for m in mounts)
    result['runtime_mode'] = runtime_mode()
    if result['runtime_mode'] == 'desktop':
        wayland_sync('/run/user/1000/wayland-0')
        result['wayland_sync'] = True
    else:
        result['wayland_sync'] = None
        result['ssh_listening'] = ssh_listening()
    result['touch_alive'] = touch_alive()
    result['wlan_present'] = Path('/sys/class/net/wlan0').exists()
    # Association loss does not force a reboot; radio/wpa/DHCP are supervised.
    result['healthy'] = result['root_rw'] and result['touch_alive'] and not Path('/run/frl/control/shutdown-in-progress').exists()
    if result['runtime_mode'] == 'server':
        result['healthy'] = result['healthy'] and result['ssh_listening']
    if 'fuel' in Path('/etc/frl/enabled-services').read_text().splitlines():
        import sys
        if '/usr/lib/frl/fuel' not in sys.path:
            sys.path.insert(0, '/usr/lib/frl/fuel')
        import health as fuel_health
        result['fuel'] = fuel_health.check()
        result['healthy'] = result['healthy'] and result['fuel']['healthy']
    return result


def check_with_boot_registration():
    result = check()
    if result['healthy']:
        result['vendor_boot'] = vendor_boot.mark_boot_success(healthy=True)
        result['healthy'] = result['vendor_boot']['registered']
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe-wayland', metavar='SOCKET')
    args = parser.parse_args()
    if args.probe_wayland:
        wayland_sync(args.probe_wayland)
        print('WAYLAND_SYNC_REPLY_VERIFIED')
        return
    directory = Path('/run/frl/control')
    acceptance = Path('/etc/frl/standalone-enabled')
    boot_recorded = False
    while True:
        try:
            result = check_with_boot_registration()
            if result['healthy'] and not boot_recorded:
                record = directory / ('vendor-boot-success.' + str(os.getpid()))
                fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as output:
                    json.dump(result['vendor_boot'], output)
                    output.write('\n')
                record.replace(directory / 'vendor-boot-success.json')
                print('FRL_NATIVE_VENDOR_BOOT_REGISTERED ' + json.dumps(result['vendor_boot']), flush=True)
                boot_recorded = True
        except Exception as error:
            result = {'monotonic': time.monotonic(), 'healthy': False,
                      'error': f'{type(error).__name__}: {error}'}
        accepted = enabled_token(acceptance, b'FRL_NATIVE_V15_ACCEPTED\n')
        # A volatile qualification request keeps this native debugging session
        # independent of the PC, without accepting future boots as qualified.
        qualifying = enabled_token(directory / 'qualification-enabled', b'FRL_NATIVE_V15_QUALIFICATION\n')
        result['standalone_enabled'] = accepted
        result['keepalive_mode'] = 'standalone' if accepted else 'qualification' if qualifying else 'host'
        if (accepted or qualifying) and result['healthy']:
            try:
                fd = os.open(directory / 'keepalive', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        temporary = directory / 'health.json.new'
        temporary.write_text(json.dumps(result, indent=2))
        temporary.replace(directory / 'health.json')
        time.sleep(10)


if __name__ == '__main__':
    main()
