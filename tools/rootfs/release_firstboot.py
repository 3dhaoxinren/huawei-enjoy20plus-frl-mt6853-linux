#!/usr/bin/python3
"""Create device-local identities before FRL services; no shared credentials."""
import os
from pathlib import Path
import re
import stat
import subprocess


def checked(root, relative):
    path = root
    for component in relative.split('/'):
        if component in ('', '.', '..'):
            raise ValueError('invalid relative path')
        path /= component
        if path.is_symlink():
            raise RuntimeError('symlink refused: ' + relative)
        if path.exists() and path.stat().st_uid != 0:
            raise RuntimeError('non-root owned path: ' + relative)
    return path


def directory(root, relative, mode=0o755):
    path = checked(root, relative)
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise RuntimeError('not a directory: ' + relative)
    os.chmod(path, mode)
    return path


def write_new(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def identity(root, relative, pattern, generator):
    path = checked(root, relative)
    if path.exists():
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_mode & 0o022:
            raise RuntimeError('unsafe identity: ' + relative)
        value = path.read_text().strip()
        if value:
            if not re.fullmatch(pattern, value):
                raise RuntimeError('invalid existing identity: ' + relative)
            return
        # The image ships an empty machine-id only. No populated identity is replaced.
        path.unlink()
    write_new(path, (generator() + '\n').encode('ascii'), 0o644)


def initialize(root, run=subprocess.run):
    root = Path(root)
    directory(root, 'etc/ssh', 0o755)
    directory(root, 'etc/frl/network', 0o755)
    directory(root, 'etc/frl/ssh/authorized_keys', 0o755)
    directory(root, 'etc/wpa_supplicant', 0o755)
    identity(root, 'etc/machine-id', r'[0-9a-f]{32}', lambda: os.urandom(16).hex())
    identity(root, 'etc/frl/network/wlan-mac', r'02(:[0-9a-f]{2}){5}',
             lambda: '02:' + ':'.join(f'{b:02x}' for b in os.urandom(5)))
    for kind in ('ed25519', 'ecdsa'):
        path = checked(root, 'etc/ssh/ssh_host_' + kind + '_key')
        public = checked(root, 'etc/ssh/ssh_host_' + kind + '_key.pub')
        if not path.exists():
            if public.exists():
                raise RuntimeError('orphan host public key requires local review')
            run(['/usr/bin/ssh-keygen', '-q', '-t', kind, '-N', '', '-f', str(path)], check=True)
        if not path.is_file() or path.stat().st_nlink != 1 or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise RuntimeError('unsafe host private key permissions')
        # Verify private key readability and repair a missing .pub after interruption.
        key = run(['/usr/bin/ssh-keygen', '-y', '-P', '', '-f', str(path)],
                  check=True, capture_output=True).stdout.strip()
        if public.exists():
            if not public.is_file() or public.stat().st_nlink != 1 or public.stat().st_mode & 0o022:
                raise RuntimeError('unsafe host public key')
            if public.read_bytes().split()[:2] != key.split()[:2]:
                raise RuntimeError('host key pair mismatch')
        else:
            write_new(public, key + b'\n', 0o644)
    config = checked(root, 'etc/wpa_supplicant/frl-wlan0.conf')
    if not config.exists():
        write_new(config, b'ctrl_interface=/run/wpa_supplicant\nupdate_config=1\n', 0o600)
    elif not config.is_file() or config.stat().st_mode & 0o077:
        raise RuntimeError('unsafe Wi-Fi configuration')


def main():
    if os.geteuid() != 0 or Path('/proc/1/comm').read_text().strip() != 'frl-init':
        raise RuntimeError('native FRL root required')
    if not os.path.samefile('/', '/proc/1/root'):
        raise RuntimeError('not the native root filesystem')
    if Path('/sys/firmware/devicetree/base/compatible').read_bytes() != b'mediatek,MT6853\0':
        raise RuntimeError('unexpected device')
    initialize(Path('/'))
    print('FRL_RELEASE_DEVICE_IDENTITIES_READY; SSH requires local public-key enrollment')


if __name__ == '__main__':
    main()
