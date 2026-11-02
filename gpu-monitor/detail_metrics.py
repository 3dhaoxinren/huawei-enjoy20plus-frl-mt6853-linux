"""Bounded read-only detail telemetry; shares the existing three-second sample."""
import math
import os
import re
from collections import deque
from metrics import Sampler, read_text, counter_rate


def pressure(raw):
    result = {}
    for line in raw.splitlines():
        fields = line.split()
        if not fields or fields[0] not in ('some', 'full'):
            continue
        row = dict(item.split('=', 1) for item in fields[1:] if '=' in item)
        value = float(row['avg10'])
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('invalid pressure')
        result[fields[0]] = value
    if 'some' not in result:
        raise ValueError('missing pressure')
    return result


def gpu_state(raw):
    """MT6853 vendor dump. Report kHz only from the explicitly real clock field."""
    real = re.search(r'\(real\) freq:\s*(\d+)', raw)
    count = re.search(r'g_power_count\s*=\s*(\d+)', raw)
    if not real or not count:
        raise ValueError('unrecognized GPU state')
    frequency, powered = int(real[1]), int(count[1]) > 0
    if not 0 <= frequency <= 3000000:
        raise ValueError('invalid GPU frequency')
    return {'powered': powered, 'frequency_khz': frequency if powered and frequency > 0 else None}


class DetailSampler(Sampler):
    def __init__(self, root='/', interface='wlan0'):
        super().__init__(root, interface)
        self.old_vm = None
        self.memory_history = deque(maxlen=21)

    def extra(self, stamp, data):
        result = {'cpu_policies': [], 'pressure': {}, 'volumes': [], 'gpu_state': None,
                  'gpu_memory': None, 'swap_in': None, 'swap_out': None, 'major_faults': None}
        for p in sorted(self.path('/sys/devices/system/cpu/cpufreq').glob('policy*')):
            try:
                # Never substitute a requested P-state for hardware readback.
                freq = int(read_text(p/'cpuinfo_cur_freq', 128))
                maximum = int(read_text(p/'cpuinfo_max_freq', 128))
                related = read_text(p/'related_cpus', 128).strip()
                if not 0 < freq <= 10000000 or not 0 < maximum <= 10000000:
                    raise ValueError('invalid frequency')
                result['cpu_policies'].append({'cpus': related, 'frequency_khz': freq, 'max_khz': maximum})
            except (OSError, ValueError):
                result['cpu_policies'].append({'cpus': p.name, 'frequency_khz': None, 'max_khz': None})
        for resource in ('cpu', 'memory', 'io'):
            try:result['pressure'][resource] = pressure(read_text(self.path('/proc/pressure/'+resource), 1024))
            except (OSError, ValueError, KeyError):result['pressure'][resource] = None
        try:result['gpu_state'] = gpu_state(read_text(self.path('/proc/gpufreq/gpufreq_var_dump'), 4096))
        except (OSError, ValueError):pass
        try:
            raw = read_text(self.path('/proc/mali/memory_usage'), 32768)
            match = re.search(r'Total\s+\d+\((\d+) bytes\)', raw)
            if match:result['gpu_memory'] = int(match[1])
        except OSError:pass
        for label, path in (('系统根卷', '/'), ('数据卷', '/run/frl/storage/frl-linux')):
            row = {'label': label, 'total': None, 'available': None, 'used': None}
            try:
                v = os.statvfs(self.path(path))
                row.update(total=v.f_blocks*v.f_frsize, available=v.f_bavail*v.f_frsize,
                           used=(v.f_blocks-v.f_bfree)*v.f_frsize)
            except OSError:pass
            result['volumes'].append(row)
        try:
            vm = dict(line.split() for line in read_text(self.path('/proc/vmstat')).splitlines())
            values = tuple(int(vm[key]) for key in ('pswpin', 'pswpout', 'pgmajfault'))
            if min(values) < 0:raise ValueError('negative counter')
            for i, key in enumerate(('swap_in', 'swap_out', 'major_faults')):
                old = (self.old_vm[0], self.old_vm[1][i]) if self.old_vm else None
                rate = counter_rate(old, stamp, values[i])
                result[key] = rate*self.page_size if rate is not None and i < 2 else rate
            self.old_vm = (stamp, values)
        except (OSError, ValueError, KeyError):self.old_vm = None
        total, used = data.get('memory_total'), data.get('memory_used')
        self.memory_history.append((stamp, used/total*100 if total and used is not None else None))
        result['memory_history'] = list(self.memory_history)
        return result

    def sample(self):
        data = super().sample()
        data['details'] = self.extra(data['stamp'], data)
        return data
