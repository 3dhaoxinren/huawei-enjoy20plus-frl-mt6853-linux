#!/usr/bin/python3
"""Owned IPv4 lease/routes/resolver; never flush unrelated interface state."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from frl_network import Failure, IP, RUNTIME, execute, private_directory, read_json, write_json


def ipv4(value):
    a = ipaddress.IPv4Address(value)
    if a.is_unspecified or a.is_multicast or a.is_loopback or int(a) == 0xffffffff:
        raise Failure('invalid-dhcp-address')
    return str(a)


def parse_lease(env):
    if env.get('interface') != 'wlan0':
        raise Failure('unexpected-dhcp-interface')
    address = ipv4(env.get('ip', ''))
    mask = env.get('subnet', '')
    if not mask:
        raise Failure('missing-netmask')
    prefix = ipaddress.IPv4Network('0.0.0.0/' + mask).prefixlen
    if prefix == 0:
        raise Failure('zero-prefix-dhcp-netmask')
    routers = env.get('router', '').split()
    dns = env.get('dns', '').split()
    if len(routers) > 8 or len(dns) > 6:
        raise Failure('oversized-dhcp-option')
    gateways = [ipv4(x) for x in routers]
    servers = []
    for value in dns:
        a = ipaddress.ip_address(value)
        if a.is_unspecified or a.is_multicast or a.is_loopback:
            raise Failure('invalid-dns-address')
        servers.append(str(a))
    return {'ip': address, 'prefix': prefix, 'gateway': gateways[0] if gateways else None,
            'dns': servers}


def valid_lease(value):
    try:
        parsed = parse_lease({'interface': 'wlan0', 'ip': value['ip'],
                              'subnet': str(value['prefix']),
                              'router': value.get('gateway') or '',
                              'dns': ' '.join(value['dns'])})
        return parsed == value
    except (ValueError, KeyError, TypeError, Failure):
        return False


class LeaseManager:
    def __init__(self, runtime=RUNTIME, resolver=Path('/etc/resolv.conf'), command=execute,
                 dnsfile=Path('/run/frl/resolver/resolv.conf')):
        self.runtime, self.resolver, self.command = runtime, resolver, command
        private_directory(runtime)
        self.statefile = runtime / 'lease.json'
        self.dnsfile = dnsfile
        dnsfile.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        s = dnsfile.parent.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid() or s.st_mode & 0o022:
            raise Failure('resolver-directory-ownership')

    def check_resolver(self):
        if not self.resolver.is_symlink() or os.readlink(self.resolver) != str(self.dnsfile):
            raise Failure('resolver-symlink-not-integrated')

    def dns_write(self, servers):
        fd, temporary = tempfile.mkstemp(prefix='.dns-', dir=self.dnsfile.parent)
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, 'w') as f:
                f.write('# Managed by FRL wlan0 DHCP\n')
                for server in servers:
                    f.write('nameserver ' + server + '\n')
            os.replace(temporary, self.dnsfile)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def route(self, verb, lease):
        if lease.get('gateway'):
            self.command([IP, '-4', 'route', verb, 'default', 'via', lease['gateway'],
                          'dev', 'wlan0', 'metric', '600', 'proto', 'dhcp'])

    def check_route_ownership(self, owned):
        routes = json.loads(self.command([IP, '-j', '-4', 'route', 'show', 'dev', 'wlan0'], capture=True))
        for r in routes:
            if r.get('dst') == 'default' and r.get('metric') == 600:
                if r.get('protocol') not in ('dhcp', 16, '16') or not any(
                        p.get('gateway') == r.get('gateway') for p in owned):
                    raise Failure('unowned-default-route-at-metric-600')

    def drop(self, lease, keep=None):
        # Ignore only absent state (ip command reports failure). Preserve a record
        # if any deletion fails so the next deconfig can retry; never broad flush.
        failures = []
        if lease.get('gateway') and (not keep or lease['gateway'] != keep.get('gateway')):
            try:
                routes = json.loads(self.command([IP, '-j', '-4', 'route', 'show', 'dev', 'wlan0', 'proto', 'dhcp'], capture=True))
                if any(r.get('dst') == 'default' and r.get('gateway') == lease['gateway']
                       and r.get('metric') == 600 for r in routes):
                    self.route('del', lease)
            except Failure:
                failures.append('route')
        if not keep or (lease['ip'], lease['prefix']) != (keep['ip'], keep['prefix']):
            try:
                links = json.loads(self.command([IP, '-j', '-4', 'addr', 'show', 'dev', 'wlan0'], capture=True))
                if any(a.get('local') == lease['ip'] and a.get('prefixlen') == lease['prefix']
                       for link in links for a in link.get('addr_info', [])):
                    self.command([IP, '-4', 'addr', 'del', f"{lease['ip']}/{lease['prefix']}", 'dev', 'wlan0'])
            except Failure:
                failures.append('address')
        return failures

    def apply(self, action, env):
        if env.get('interface') != 'wlan0':
            raise Failure('unexpected-dhcp-interface')
        if action not in ('bound', 'renew', 'deconfig'):
            return
        new = parse_lease(env) if action != 'deconfig' else None
        self.check_resolver()
        fd = os.open(self.runtime / 'lease.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Failure('lease-lock-deadline')
                    time.sleep(0.05)
            state = read_json(self.statefile)
            old = []
            for value in [state.get('current'), *state.get('owned', [])]:
                if value:
                    if not valid_lease(value):
                        raise Failure('invalid-owned-lease-record')
                    if value not in old:
                        old.append(value)
            if new:
                self.check_route_ownership(old)
                # Save intent first, so an interrupted hook is still cleanable.
                write_json(self.statefile, {'owned': old + ([new] if new not in old else []), 'generation': int(state.get('generation', 0))})
                self.command([IP, '-4', 'addr', 'replace', f"{new['ip']}/{new['prefix']}", 'dev', 'wlan0'])
                self.route('replace', new)
                for lease in old:
                    if self.drop(lease, new):
                        raise Failure('previous-lease-cleanup-incomplete')
                self.dns_write(new['dns'])
                write_json(self.statefile, {'current': new, 'owned': [new], 'generation': int(state.get('generation', 0)) + 1, 'last_action': action, 'updated_monotonic': time.monotonic()})
            else:
                failed = False
                for value in old:
                    failed = bool(self.drop(value)) or failed
                self.dns_write([])
                if failed:
                    # Already-absent entries are verified separately by native integration;
                    # failure is surfaced rather than falsely claiming an empty lease.
                    raise Failure('owned-lease-cleanup-incomplete')
                write_json(self.statefile, {'generation': int(state.get('generation', 0)), 'last_action': 'deconfig'})
        finally:
            os.close(fd)
