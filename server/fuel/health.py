"""Read-only genuine process/socket/initialization health; never sends netlink."""
import hashlib
import errno
import gzip
import io
import zlib
import json
import os
from pathlib import Path
import stat

RUNTIME = Path('/opt/frl-fuel-vendor')
IDENTITY = Path('/run/frl/fuel/production-child.json')
DAEMON_SHA = '15e4a40eca25182cca9530a1e039e31f790ce5ff03aeda2d4f52cbe7622f503b'


def proc(pid):
    p = Path('/proc')/str(pid)
    f = (p/'stat').read_text().rsplit(')',1)[1].split()
    return {'pid':pid,'state':f[0],'ppid':int(f[1]),'pgid':int(f[2]),'sid':int(f[3]),'start':int(f[19])}


def identity_matches(record, actual, boot_id):
    return (record['boot_id'] == boot_id and actual['state'] not in ('Z','X')
            and actual['pid'] == record['pid'] and actual['start'] == record['start']
            and actual['sid'] == actual['pgid'] == record['pid']
            and actual['ppid'] == record['supervisor_pid'])


def config_disables_netns(compressed):
    if len(compressed) > 262144:
        raise ValueError('running kernel config compressed limit exceeded')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            plain = stream.read(524289)
    except (EOFError, zlib.error) as error:
        raise ValueError('invalid running kernel config gzip') from error
    if len(plain) > 524288:
        raise ValueError('running kernel config expanded limit exceeded')
    lines = set(plain.decode('ascii').splitlines())
    # Complete keys only: CONFIG_NET_NSH is unrelated to CONFIG_NET_NS.
    net_values = [line for line in lines if line.startswith('CONFIG_NET_NS=')]
    if net_values:
        return False  # Enabled, contradictory, or unrecognized explicit value.
    if '# CONFIG_NET_NS is not set' in lines:
        return True
    return ('# CONFIG_NAMESPACES is not set' in lines and
            not any(line.startswith('CONFIG_NAMESPACES=') for line in lines))


def runtime_netns_disabled():
    fd = os.open('/proc/config.gz', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            return config_disables_netns(stream.read(262145))
    finally:
        os.close(fd)


def network_namespace_matches(process_path):
    paths = (process_path/'ns/net', Path('/proc/1/ns/net'))
    observed = []
    for path in paths:
        try:
            observed.append(path.stat())
        except FileNotFoundError as error:
            if error.errno != errno.ENOENT:
                raise
            observed.append(None)
    if all(item is not None for item in observed):
        return os.path.samestat(*observed)
    if any(item is not None for item in observed):
        return False
    # Missing processes/ns directories are not evidence of a disabled feature.
    if not all(stat.S_ISDIR(path.parent.stat().st_mode) for path in paths):
        return False
    return runtime_netns_disabled()


def process_valid(record):
    p = Path('/proc')/str(record['pid'])
    actual = proc(record['pid'])
    if not identity_matches(record,actual,Path('/proc/sys/kernel/random/boot_id').read_text().strip()):
        return False
    parent = proc(record['supervisor_pid'])
    if parent['start'] != record['supervisor_start'] or parent['state'] in ('Z','X'):
        return False
    ids = {line.split(':',1)[0]:line.split()[1:] for line in (p/'status').read_text().splitlines() if line.startswith(('Uid:','Gid:'))}
    if ids != {'Uid':['1000']*4,'Gid':['1000']*4}:
        return False
    return (os.path.samefile(p/'root',RUNTIME) and os.path.samefile(p/'exe',RUNTIME/'vendor/bin/fuelgauged')
            and network_namespace_matches(p)
            and hashlib.sha256((p/'exe').read_bytes()).hexdigest() == DAEMON_SHA)


def socket_ready(pid):
    sockets = set()
    for descriptor in (Path('/proc')/str(pid)/'fd').iterdir():
        try:sockets.add(os.readlink(descriptor))
        except FileNotFoundError:continue
    for row in Path('/proc/net/netlink').read_text().splitlines()[1:]:
        f = row.split()
        if len(f) >= 10 and f[1] == '26' and int(f[2]) == pid and 'socket:['+f[-1]+']' in sockets:
            return True
    return False


def initialized(record):
    proof = record['initialization']
    return (proof.get('registered_pid') == record['pid'] and proof.get('uisoc_after_registration') is True
            and proof.get('gm30_disabled') == 0 and not proof.get('log_gap',False))


def check():
    result = {'healthy':False}
    try:
        s = IDENTITY.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or stat.S_IMODE(s.st_mode) != 0o600:
            return result
        record = json.loads(IDENTITY.read_text())
        if record['schema'] != 'FRL_FUEL_CHILD_1' or not process_valid(record):
            return result
        fresh = initialized(record)
        capacity = int(Path('/sys/class/power_supply/battery/capacity').read_text().strip())
        connected = socket_ready(record['pid'])
        result.update(pid=record['pid'],start=record['start'],capacity=capacity,netlink26=connected,
                      initialized=fresh,healthy=fresh and connected and 0 <= capacity <= 100)
    except (OSError,ValueError,KeyError,TypeError):
        pass
    return result


if __name__ == '__main__':
    print(json.dumps(check()))
