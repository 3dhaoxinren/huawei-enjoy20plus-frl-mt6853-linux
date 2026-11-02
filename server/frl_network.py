#!/usr/bin/python3
"""Foreground FRL Wi-Fi services. No credentials are read into supervisor logs."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

if __name__ == '__main__':
    sys.modules['frl_network'] = sys.modules[__name__]

BB = '/usr/lib/frl/busybox'
IP = '/usr/sbin/ip'
VENDOR = Path('/opt/frl-vendor')
RUNTIME = Path('/run/frl/network')
MODULE = '/usr/lib/frl/network/frl_network.py'
HOOK = '/usr/lib/frl/network/udhcpc-hook.sh'
HOTPLUG = '/usr/lib/frl/network/firmware-hotplug.sh'
WPA_CONFIG = Path('/etc/wpa_supplicant/frl-wlan0.conf')
BRIDGE_SHA256 = '819b2ef6123d91678a2119f4a030fd176c9085dd7dc47d7b966b2ea20aceb5d0'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C'}


class Failure(Exception):
    pass


class Stopping(Exception):
    pass


def native_guard():
    if os.geteuid() != 0 or os.uname().machine != 'aarch64':
        raise Failure('native-arm64-root-required')


def identity(pid):
    try:
        parts = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': int(pid), 'start': int(parts[19]), 'group': int(parts[2]),
                'session': int(parts[3]), 'state': parts[0]}
    except (OSError, ValueError, IndexError):
        return None


def alive(record):
    current = identity(record.get('pid', -1))
    return bool(current and current['state'] != 'Z' and current['start'] == record.get('start'))


def group_members(group):
    return [r for p in Path('/proc').glob('[0-9]*') if (r := identity(p.name))
            and r['group'] == group and r['session'] == group and r['state'] != 'Z']


def launcher_opened(pid):
    try:
        wanted = Path('/dev/stpwmt').stat().st_rdev
        for fd in Path(f'/proc/{pid}/fd').iterdir():
            try:
                s = fd.stat()
                if stat.S_ISCHR(s.st_mode) and s.st_rdev == wanted:
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid() or s.st_mode & 0o077:
        raise Failure('runtime-directory-not-private')


def read_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd) as f:
        return json.load(f)


def write_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def execute(args, capture=False, timeout=5):
    try:
        p = subprocess.run(args, env=ENV, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout, text=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Failure('local-command-timeout-or-unavailable') from e
    if p.returncode:
        raise Failure('local-command-failed')
    return p.stdout if capture else ''


def association():
    try:
        text = execute(['/usr/sbin/wpa_cli', '-p', '/run/wpa_supplicant', '-i', 'wlan0', 'status'], True, 2)
        return 'wpa_state=COMPLETED' in text.splitlines()
    except Failure:
        return False


def role_alive(role, runtime=RUNTIME):
    try:
        s = read_json(runtime / (role + '.json'))
        return (s.get('phase') == 'ready' and alive(s.get('owner', {})) and
                any(alive(p) for p in s.get('children', [])))
    except (OSError, ValueError, TypeError):
        return False


def radio_ready():
    return role_alive('radio') and Path('/sys/class/net/wlan0').is_dir()


def managed_ipv4():
    try:
        from lease import valid_lease
        if read_json(RUNTIME / 'dhcp.json').get('renew_pending'):
            return False
        lease = read_json(RUNTIME / 'lease.json').get('current')
        if not lease or not valid_lease(lease):
            return False
        data = json.loads(execute([IP, '-j', '-4', 'addr', 'show', 'dev', 'wlan0'], True, 2))
        address_ok = any(a.get('local') == lease['ip'] and a.get('prefixlen') == lease['prefix']
                         and a.get('scope') == 'global' for d in data for a in d.get('addr_info', []))
        routes = json.loads(execute([IP, '-j', '-4', 'route', 'show', 'default', 'dev', 'wlan0'], True, 2))
        route_ok = not lease.get('gateway') or any(r.get('gateway') == lease['gateway'] and r.get('metric') == 600 and r.get('protocol') in ('dhcp', 16, '16') for r in routes)
        dns = [line.split()[1] for line in Path('/run/frl/resolver/resolv.conf').read_text().splitlines() if line.startswith('nameserver ')]
        return address_ok and route_ok and dns == lease['dns']
    except (Failure, OSError, ValueError, TypeError):
        return False


def health(role='network'):
    state = {'radio': radio_ready(), 'wpa': role_alive('wpa'),
             'dhcp': role_alive('dhcp'), 'association': association(), 'ipv4': managed_ipv4()}
    needed = {'radio': ['radio'], 'wpa': ['radio', 'wpa', 'association'],
              'dhcp': ['radio', 'wpa', 'dhcp', 'association', 'ipv4'],
              'network': list(state)}[role]
    return all(state[k] for k in needed), state


def credential_file(path=WPA_CONFIG):
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
        raise Failure('credential-parent-permissions')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1:
            raise Failure('credential-file-must-be-root-0600-regular-single-link')
    finally:
        os.close(fd)


def character_major(text, driver):
    section = False
    matches = []
    for line in text.splitlines():
        if line == 'Character devices:':
            section = True
        elif line == 'Block devices:':
            section = False
        elif section:
            fields = line.split()
            if len(fields) == 2 and fields[1] == driver:
                matches.append(int(fields[0]))
    if len(matches) != 1:
        raise Failure('character-driver-not-uniquely-registered')
    return matches[0]


def device_node(name, expected_major, driver):
    candidates = list(Path('/sys/class').glob('*/' + name + '/dev'))
    values = {p.read_text().strip() for p in candidates}
    if values:
        if len(values) != 1:
            raise Failure('ambiguous-sysfs-device')
        major, minor = map(int, next(iter(values)).split(':'))
    else:
        major = character_major(Path('/proc/devices').read_text(), driver)
        if major != expected_major:
            raise Failure('fallback-major-does-not-match-observed-driver')
        minor = 0  # Only the observed WMT single-device minor, with registered major assertion.
    path = Path('/dev') / name
    wanted = os.makedev(major, minor)
    if path.exists() or path.is_symlink():
        s = path.lstat()
        if not stat.S_ISCHR(s.st_mode) or s.st_rdev != wanted:
            raise Failure('existing-device-node-mismatch')
    else:
        os.mknod(path, stat.S_IFCHR | 0o600, wanted)


def bind(source, target):
    if not source.is_dir():
        raise Failure('bind-source-missing')
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise Failure('bind-target-is-symlink')
    # Mountinfo uses /usr/lib/firmware for Ubuntu's /lib parent symlink.
    target = target.resolve(strict=True)
    source = source.resolve(strict=True)
    points = {l.split()[4] for l in Path('/proc/self/mountinfo').read_text().splitlines()}
    if str(target) in points:
        a, b = source.stat(), target.stat()
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise Failure('existing-bind-conflicts')
    else:
        execute([BB, 'mount', '--bind', str(source), str(target)])


def prepare_vendor():
    for rel in ('vendor/bin/wmt_loader', 'vendor/bin/wmt_launcher', 'system/bin/linker64', 'libfrlprops.so'):
        if not (VENDOR / rel).is_file():
            raise Failure('verified-vendor-resource-missing')
    if hashlib.sha256((VENDOR / 'libfrlprops.so').read_bytes()).hexdigest() != BRIDGE_SHA256:
        raise Failure('wmt-v1-property-bridge-hash-mismatch')
    for part in ('dev', 'proc', 'sys'):
        bind(Path('/') / part, VENDOR / part)
    bind(VENDOR / 'vendor', Path('/vendor'))
    bind(Path('/vendor/firmware'), Path('/lib/firmware'))
    bind(Path('/vendor/firmware'), VENDOR / 'lib/firmware')
    Path('/dev/socket').mkdir(exist_ok=True)
    Path('/dev/frl-props').mkdir(mode=0o700, exist_ok=True)
    previous = Path('/proc/sys/kernel/hotplug').read_text().strip()
    if previous not in ('', HOTPLUG):
        raise Failure('different-hotplug-owner-requires-integration')
    Path('/sys/module/firmware_class/parameters/path').write_text('/vendor/firmware')
    Path('/proc/sys/kernel/hotplug').write_text(HOTPLUG)
    if Path('/sys/module/firmware_class/parameters/path').read_text().strip() != '/vendor/firmware':
        raise Failure('firmware-path-readback')


def foreign_daemon(role):
    names = {'radio': {'wmt_loader', 'wmt_launcher'}, 'wpa': {'wpa_supplicant'}, 'dhcp': {'udhcpc'}}[role]
    for p in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = p.read_bytes().split(b'\0')
            first = os.path.basename(os.fsdecode(args[0]))
            if first == 'busybox' and len(args) > 1:
                first = os.fsdecode(args[1])
                if first == 'chroot' and len(args) > 3:
                    first = os.path.basename(os.fsdecode(args[3]))
            if first in names:
                if role == 'radio' or b'wlan0' in args or b'-iwlan0' in args:
                    return True
        except (OSError, ValueError):
            continue
    return False


class Supervisor:
    def __init__(self, role, runtime=RUNTIME):
        self.role, self.runtime = role, runtime
        private_directory(runtime)
        self.lock = os.open(runtime / (role + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(self.lock)
            raise Failure('role-already-supervised') from e
        self.path = runtime / (role + '.json')
        self.state = read_json(self.path)
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if self.state and self.state.get('boot_id') != boot_id:
            self.state = {}
        from network_recovery import reap_orphans
        try:
            reap_orphans(role, self.state)
        except BaseException:
            os.close(self.lock)
            raise
        self.stop = False
        self.children = []
        self.state.update(owner=identity(os.getpid()), children=[], phase='starting', boot_id=boot_id, renew_pending=False)
        self.save()

    def save(self):
        write_json(self.path, self.state)

    def signal(self, *_):
        self.stop = True

    def check_stop(self):
        if self.stop:
            raise Stopping()

    def spawn(self, args, role, env=ENV):
        self.check_stop()
        # No raw child output: WPA parse errors/debug output must never expose a secret.
        # nosec: PATH_TRAVERSAL — fixed internal argv lists, shell=False; this is Popen, not file open.
        p = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        self.children.append(p)
        record = identity(p.pid)
        if record:
            record['role'] = role
            self.state['children'].append(record)
        self.save()
        return p

    def wait_child(self, p, seconds):
        deadline = time.monotonic() + seconds
        while p.poll() is None:
            self.check_stop()
            if time.monotonic() >= deadline:
                raise Failure('child-deadline-expired')
            time.sleep(0.1)
        if p.returncode:
            raise Failure('child-exit-nonzero')

    def wait_ready(self, predicate, seconds, required=None):
        deadline = time.monotonic() + seconds
        while not predicate():
            self.check_stop()
            if required is not None and required.poll() is not None:
                raise Failure('required-child-exited')
            if time.monotonic() >= deadline:
                raise Failure('readiness-deadline-expired')
            time.sleep(0.25)

    def ready(self):
        self.state['phase'] = 'ready'
        self.save()
        print(f'{self.role}: local readiness passed', flush=True)

    def monitor(self, child, predicate):
        bad_since = None
        while True:
            self.check_stop()
            if child.poll() is not None:
                raise Failure('foreground-daemon-exited')
            if predicate():
                bad_since = None
            elif bad_since is None:
                bad_since = time.monotonic()
            elif time.monotonic() - bad_since > 10:
                raise Failure('local-dependency-lost')
            time.sleep(0.5)

    def cleanup(self, keep_lock=False):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for p in self.children:
                # Session descendants are ours even when the foreground leader exited.
                if p.poll() is None or group_members(p.pid):
                    try:
                        os.killpg(p.pid, sig)
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + 2
            while any(p.poll() is None or group_members(p.pid) for p in self.children) and time.monotonic() < deadline:
                time.sleep(0.05)
        remaining = [r for p in self.children for r in group_members(p.pid)]
        self.state.update(children=remaining, phase='blocked-orphan' if remaining else 'stopped')
        self.save()
        if not keep_lock:
            os.close(self.lock)
        return not remaining


def radio(s):
    if foreign_daemon('radio'):
        raise Failure('unowned-wmt-process-present')
    if not s.state.get('loader_attempted') and Path('/sys/class/net/wlan0').exists():
        raise Failure('unowned-existing-radio-no-blind-reload')
    if s.state.get('loader_attempted') and not s.state.get('loader_complete'):
        raise Failure('loader-attempt-latched-operator-review-required')
    if s.state.get('power_attempted') and not Path('/sys/class/net/wlan0').exists():
        raise Failure('power-attempt-latched-no-blind-retry')
    prepare_vendor()
    env = dict(ENV, LD_PRELOAD='/libfrlprops.so', LD_LIBRARY_PATH='/system/lib64')
    if not s.state.get('loader_complete'):
        if s.state.get('loader_attempted'):
            raise Failure('loader-attempt-latched-operator-review-required')
        device_node('wmtdetect', 154, 'mtk_wcn_detect')
        s.state['loader_attempted'] = True
        s.save()
        p = s.spawn([BB, 'chroot', str(VENDOR), '/vendor/bin/wmt_loader'], 'loader', env)
        s.wait_child(p, 40)  # Natural exit; never force a ready property.
        s.state['loader_complete'] = True
        s.save()
    device_node('stpwmt', 190, 'mtk_stp_wmt')
    device_node('wmtWifi', 153, 'mtk_wmt_wifi_chrdev')
    launcher = s.spawn([BB, 'chroot', str(VENDOR), '/vendor/bin/wmt_launcher',
                        '-p', '/vendor/firmware/', '-o', '1'], 'launcher', env)
    s.wait_ready(lambda: launcher.poll() is None and launcher_opened(launcher.pid), 10, launcher)
    if not Path('/sys/class/net/wlan0').exists():
        if s.state.get('power_attempted'):
            raise Failure('power-attempt-latched-no-blind-retry')
        s.state['power_attempted'] = True
        s.save()
        power = s.spawn([BB, 'sh', '-c', 'printf 1 > /dev/wmtWifi'], 'power-write')
        s.wait_child(power, 30)
    s.wait_ready(lambda: Path('/sys/class/net/wlan0').is_dir(), 15, launcher)
    if launcher.poll() is not None:
        raise Failure('launcher-not-running')
    from network_identity import apply_identity
    s.state['network_identity'] = apply_identity()
    s.save()
    execute([IP, 'link', 'set', 'dev', 'wlan0', 'up'])
    s.ready()
    s.monitor(launcher, lambda: Path('/sys/class/net/wlan0').is_dir())


def wpa(s):
    s.wait_ready(radio_ready, 45)
    credential_file()
    if foreign_daemon('wpa'):
        raise Failure('unowned-wpa-process-present')
    private_directory(Path('/run/wpa_supplicant'))
    p = s.spawn(['/usr/sbin/wpa_supplicant', '-D', 'nl80211', '-i', 'wlan0',
                 '-c', str(WPA_CONFIG), '-O', '/run/wpa_supplicant', '-qq'], 'wpa')
    s.wait_ready(lambda: radio_ready() and association(), 60, p)
    s.ready()
    s.monitor(p, lambda: radio_ready() and association())


def dhcp(s):
    from lease import LeaseManager
    s.wait_ready(lambda: role_alive('wpa') and association(), 60)
    if foreign_daemon('dhcp'):
        raise Failure('unowned-dhcp-process-present')
    lease = LeaseManager()
    lease.apply('deconfig', {'interface': 'wlan0'})
    p = s.spawn([BB, 'udhcpc', '-f', '-n', '-i', 'wlan0', '-t', '6', '-T', '3',
                 '-s', HOOK], 'udhcpc')
    s.wait_ready(managed_ipv4, 30, p)
    s.ready()
    from network_recovery import AssociationRenewal
    s.monitor(p, AssociationRenewal(s, p))


def main():
    try:
        native_guard()
        action = sys.argv[1:]
        if len(action) == 2 and action[0] == 'health' and action[1] in ('radio', 'wpa', 'dhcp', 'network'):
            ok, state = health(action[1])
            print(json.dumps(state, sort_keys=True))
            return 0 if ok else 1
        if len(action) != 2 or action[0] != 'service' or action[1] not in ('radio', 'wpa', 'dhcp'):
            return 64
        role = action[1]
        s = Supervisor(role)
        signal.signal(signal.SIGTERM, s.signal)
        signal.signal(signal.SIGINT, s.signal)
        result = 1
        try:
            {'radio': radio, 'wpa': wpa, 'dhcp': dhcp}[role](s)
        except Stopping:
            result = 0
        finally:
            try:
                clean = s.cleanup(keep_lock=True)
                if role == 'dhcp' and clean:
                    from lease import LeaseManager
                    LeaseManager().apply('deconfig', {'interface': 'wlan0'})
                if not clean:
                    result = 78
            finally:
                os.close(s.lock)
        return result
    except (Failure, OSError, ValueError) as e:
        # Messages are fixed codes; never echo exception text from config/commands.
        print('network service failed: ' + (str(e) if isinstance(e, Failure) else type(e).__name__), flush=True)
        time.sleep(2)
        return 78


if __name__ == '__main__':
    sys.exit(main())
