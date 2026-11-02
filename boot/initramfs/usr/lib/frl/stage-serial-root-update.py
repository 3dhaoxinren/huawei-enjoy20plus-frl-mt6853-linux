#!/usr/bin/python3
"""Install only the three reviewed serial handoff files before switch_root.

Production has no options: root is / and sources are beside this script in the
read-only initramfs bind. No subprocess, archive, device or reboot operation.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys

PINNED_INIT = '2c198d27cd368940a548368a11405a55edd582cc2ae58e8ebcfbdf5614f447f4'
TARGETS = ('usr/lib/frl/serial-console.sh', 'usr/lib/frl/serial-session.sh',
           'usr/lib/frl/serial-session')
CHECKS = 'etc/frl/root-payload.sha256'
BACKUP = 'var/backups/frl/serial-fd-fix-20261031'
FIELDS = {'source_basename', 'old_sha256', 'sha256', 'mode'}
HEX = re.compile(r'[0-9a-f]{64}\Z')
RELATIVE = re.compile(r'[A-Za-z0-9_.+@-]+(?:/[A-Za-z0-9_.+@-]+)*\Z')
BASENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
ALIASES = {'bin': 'usr/bin', 'sbin': 'usr/sbin', 'lib': 'usr/lib', 'lib64': 'usr/lib64'}


class Refused(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def relative_name(name):
    require(isinstance(name, str) and RELATIVE.fullmatch(name)
            and all(p not in ('.', '..') for p in name.split('/')), 'unsafe-relative-path')
    return name


def directory(path):
    s = path.lstat()
    require(stat.S_ISDIR(s.st_mode) and s.st_uid == 0
            and not s.st_mode & 0o022, 'unsafe-directory: ' + str(path))


def rooted(root, relative, create_parents=False):
    """Only known top-level usrmerge aliases are permitted; final symlinks are not."""
    relative_name(relative)
    current = root
    parts = relative.split('/')
    for i, part in enumerate(parts[:-1]):
        next_path = current / part
        if next_path.is_symlink():
            require(i == 0 and part in ALIASES
                    and os.readlink(next_path) == ALIASES[part], 'parent-symlink-refused')
            current = root
            for alias_part in ALIASES[part].split('/'):
                current /= alias_part
                directory(current)
            continue
        try:
            directory(next_path)
        except FileNotFoundError:
            require(create_parents, 'parent-directory-absent')
            next_path.mkdir(mode=0o700)
            os.chmod(next_path, 0o700)
            fsync_dir(current)
            directory(next_path)
        current = next_path
    final = current / parts[-1]
    require(not final.is_symlink(), 'final-symlink-refused: ' + relative)
    return final


def read_regular(path, single_link=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        require(stat.S_ISREG(s.st_mode) and s.st_uid == 0
                and not s.st_mode & 0o022, 'unsafe-file: ' + str(path))
        require(not single_link or s.st_nlink == 1, 'hardlinked-target-refused')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            data = f.read()
        return data, stat.S_IMODE(s.st_mode)
    finally:
        os.close(fd)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data, mode):
    directory(path.parent)
    require(not path.is_symlink(), 'atomic-target-symlink')
    temp = path.with_name('.' + path.name + '.serial-stage-' + str(os.getpid()))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, mode)  # Exact permissions even under bootstrap umask 077.
        with os.fdopen(fd, 'wb', closefd=False) as f:
            f.write(data)
            f.flush()
            os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        # Only our own O_EXCL-created staging inode, never an existing user file.
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate-json-key')
        result[key] = value
    return result


def static_arm64(data):
    require(len(data) >= 64 and data[:6] == b'\x7fELF\x02\x01'
            and struct.unpack_from('<H', data, 18)[0] == 183, 'helper-not-arm64-elf64')
    off = struct.unpack_from('<Q', data, 32)[0]
    size, count = struct.unpack_from('<HH', data, 54)
    require(size == 56 and 0 < count <= 128 and off + size * count <= len(data), 'helper-bad-program-headers')
    load = False
    for i in range(count):
        kind, _, start, _, _, length, _, _ = struct.unpack_from('<IIQQQQQQ', data, off + i * size)
        require(kind != 3, 'helper-has-interpreter')
        load |= kind == 1
        if kind == 2:
            require(start + length <= len(data) and length % 16 == 0, 'helper-bad-dynamic-table')
            for pos in range(start, start + length, 16):
                tag = struct.unpack_from('<q', data, pos)[0]
                if tag == 0:
                    break
                require(tag != 1, 'helper-has-needed-library')
    require(load, 'helper-missing-load-segment')


def read_manifest(source):
    directory(source)
    data, _ = read_regular(source / 'serial-update-manifest.json', True)
    manifest = json.loads(data, object_pairs_hook=unique_object)
    require(isinstance(manifest, dict) and set(manifest) == set(TARGETS), 'manifest-target-whitelist')
    basenames = set()
    blobs = {}
    for relative in TARGETS:
        entry = manifest[relative]
        require(isinstance(entry, dict) and set(entry) == FIELDS, 'manifest-entry-fields')
        name = entry['source_basename']
        require(isinstance(name, str) and BASENAME.fullmatch(name)
                and name not in basenames and name != 'serial-update-manifest.json', 'unsafe-or-duplicate-source-basename')
        basenames.add(name)
        require(isinstance(entry['sha256'], str) and HEX.fullmatch(entry['sha256']), 'invalid-new-hash')
        old = entry['old_sha256']
        require((old is None and relative == TARGETS[2])
                or (isinstance(old, str) and HEX.fullmatch(old)), 'invalid-old-hash')
        require(entry['mode'] == '0755', 'serial-mode-must-be-0755')
        blob, mode = read_regular(source / name, True)
        require(mode == int(entry['mode'], 8), 'source-mode-mismatch: ' + relative)
        require(sha(blob) == entry['sha256'], 'source-hash-mismatch: ' + relative)
        blobs[relative] = blob
    static_arm64(blobs[TARGETS[2]])
    return manifest, blobs


def parse_checks(data):
    require(data and data.endswith(b'\n'), 'checks-must-end-with-lf')
    records = []
    digests = {}
    for line in data.splitlines(keepends=True):
        require(re.fullmatch(rb'[0-9a-f]{64}  [^\r\n]+\n', line), 'malformed-checks-line')
        name = relative_name(line[66:-1].decode('ascii'))
        require(name not in digests and name != CHECKS, 'duplicate-or-self-checks-key')
        digests[name] = line[:64].decode('ascii')
        records.append((name, line))
    return records, digests


def verify_checks(root, digests):
    for name, expected in digests.items():
        blob, _ = read_regular(rooted(root, name))
        require(sha(blob) == expected, 'payload-check-mismatch: ' + name)


def update(root, source, expected_init_sha=PINNED_INIT):
    """Path/pin parameters exist solely for imported temporary-fixture tests."""
    require(os.geteuid() == 0, 'root-required')
    root, source = Path(root), Path(source)
    directory(root)
    marker, _ = read_regular(rooted(root, 'etc/frl/v15-root-staged'), True)
    require(marker in (b'FRL_V15_PAYLOAD_1', b'FRL_V15_PAYLOAD_1\n'), 'root-marker-mismatch')
    init, _ = read_regular(rooted(root, 'sbin/frl-init'), True)
    require(sha(init) == expected_init_sha, 'disk-root-init-hash-mismatch')
    manifest, blobs = read_manifest(source)
    checks_path = rooted(root, CHECKS)
    original, checks_mode = read_regular(checks_path, True)
    records, digests = parse_checks(original)
    verify_checks(root, digests)  # Every listed file, before backup or target writes.
    old_files = {}
    all_old = all_new = True
    for name in TARGETS:
        path = rooted(root, name)
        entry = manifest[name]
        try:
            blob, mode = read_regular(path, True)
            digest = sha(blob)
            old_files[name] = (blob, mode)
        except FileNotFoundError:
            blob = mode = digest = None
        all_old &= digest == entry['old_sha256'] and digests.get(name) == entry['old_sha256']
        all_new &= digest == entry['sha256'] and digests.get(name) == entry['sha256'] and mode == int(entry['mode'], 8)
    if all_new:
        return 'SERIAL_STAGE_ALREADY_CURRENT'
    require(all_old, 'unknown-old-or-partially-updated-serial-files')

    backup = rooted(root, BACKUP, create_parents=True)
    require(not backup.exists(), 'backup-already-exists-refuse-overwrite')
    backup.mkdir(mode=0o700)
    os.chmod(backup, 0o700)
    fsync_dir(backup.parent)
    saved_checks = backup / 'root-payload.sha256'
    atomic(saved_checks, original, checks_mode)
    require(read_regular(saved_checks, True)[0] == original, 'backup-checks-readback')
    for name, (blob, mode) in old_files.items():
        saved = rooted(backup, name, create_parents=True)
        atomic(saved, blob, mode)
        require(read_regular(saved, True) == (blob, mode), 'backup-file-readback')
    # Recheck after durable backups and before committing any target.
    require(read_regular(checks_path, True) == (original, checks_mode), 'checks-changed-during-preparation')
    verify_checks(root, digests)
    for name in TARGETS:
        path = rooted(root, name)
        if name not in old_files:
            require(not path.exists(), 'absent-target-appeared')
        else:
            require(read_regular(path, True) == old_files[name], 'target-changed-during-preparation')
        atomic(path, blobs[name], int(manifest[name]['mode'], 8))
        require(read_regular(path, True) == (blobs[name], int(manifest[name]['mode'], 8)), 'target-readback-mismatch')
    changed = b''.join((manifest[name]['sha256'].encode() + line[64:]) if name in manifest else line
                       for name, line in records)
    for name in TARGETS:
        if name not in digests:
            changed += manifest[name]['sha256'].encode() + b'  ' + name.encode() + b'\n'
    atomic(checks_path, changed, checks_mode)
    require(read_regular(checks_path, True) == (changed, checks_mode), 'checks-readback-mismatch')
    _, final_digests = parse_checks(changed)
    verify_checks(root, final_digests)
    return 'SERIAL_STAGE_UPDATED_AND_VERIFIED'


def main():
    require(len(sys.argv) == 1, 'no-cli-arguments-permitted')
    print(update(Path('/'), Path(__file__).absolute().parent), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (Refused, OSError, ValueError, UnicodeError, struct.error) as error:
        print('SERIAL_STAGE_REFUSED: ' + str(error), file=sys.stderr, flush=True)
        sys.exit(78)
