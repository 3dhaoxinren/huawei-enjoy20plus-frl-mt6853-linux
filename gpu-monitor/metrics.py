"""Read-only, dependency-free Linux telemetry. No shell commands or root required."""
from __future__ import annotations

import fcntl
import os
import re
import socket
import struct
import time
from collections import deque
from pathlib import Path


def read_text(path: Path, limit: int = 65536) -> str:
    with path.open(encoding="utf-8", errors="replace") as stream:
        return stream.read(limit)


def parse_gpu(text: str) -> int:
    """Mali r21p0's first field is the driver's busy/total percentage.

    CL fields are shares, not extra utilization to add. Frequency/power flags
    are not substitutes (this board reports power=0 even during real draws).
    """
    match = re.fullmatch(
        r'gpu/cljs0/cljs1=(\d{1,3})/(\d{1,3})/(\d{1,3}), frequency index=\d+ '
        r'power\(0:off, 1:0n\):[01]\s*', text)
    if not match or any(int(v) > 100 for v in match.groups()):
        raise ValueError('invalid Mali utilization')
    return int(match[1])


def parse_cpu(text: str) -> dict[str, tuple[int, int]]:
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or not (fields[0] == "cpu" or fields[0][3:].isdigit() and fields[0].startswith("cpu")):
            continue
        values = [int(v) for v in fields[1:9]]
        if len(values) < 4 or min(values) < 0:
            continue
        # guest/guest_nice are already included in user/nice; never count twice.
        result[fields[0]] = (sum(values), values[3] + (values[4] if len(values) > 4 else 0))
    return result


def cpu_percent(previous: tuple[int, int] | None, current: tuple[int, int]) -> float | None:
    if previous is None:
        return None
    total, idle = current[0] - previous[0], current[1] - previous[1]
    if total <= 0 or idle < 0 or idle > total:
        return None
    return 100.0 * (total - idle) / total


def parse_memory(text: str) -> dict:
    data = {}
    for line in text.splitlines():
        name, value = line.split(":", 1)
        parts = value.split()
        data[name] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    total = data.get("MemTotal")
    available = data.get("MemAvailable")
    swap = data.get("SwapTotal")
    free_swap = data.get("SwapFree")
    return {"memory_total": total,
            "memory_used": max(0, total - available) if total is not None and available is not None else None,
            "memory_available": available, "swap_total": swap,
            "swap_used": max(0, swap - free_swap) if swap is not None and free_swap is not None else None}


def parse_process(text: str) -> tuple[str, str, int, int, int]:
    # comm can itself contain spaces and parentheses.
    left, right = text.find("("), text.rfind(")")
    if left < 0 or right <= left:
        raise ValueError("malformed process stat")
    fields = text[right + 2:].split()
    name = "".join(c if c.isprintable() else "?" for c in text[left + 1:right])[:64]
    return name, fields[0], int(fields[11]) + int(fields[12]), int(fields[19]), max(0, int(fields[21]))


def parse_network(text: str, interface: str) -> tuple[int, int] | None:
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, values = line.split(":", 1)
        if name.strip() == interface:
            fields = values.split()
            return int(fields[0]), int(fields[8])
    return None


def counter_rate(previous: tuple[float, int] | None, stamp: float, value: int) -> float | None:
    if previous is None or stamp <= previous[0] or value < previous[1]:
        return None
    return (value - previous[1]) / (stamp - previous[0])


def bounded_number(path: Path, divisor: float, low: float, high: float) -> float | None:
    try:
        value = int(read_text(path, 128).strip()) / divisor
        return value if low <= value <= high else None
    except (OSError, ValueError):
        return None


class Sampler:
    def __init__(self, root: str | Path = "/", interface: str = "wlan0"):
        self.root = Path(root)
        self.interface = interface
        self.old_cpu = {}
        self.old_processes = {}
        self.old_network = None
        self.last_stamp = None
        self.history = deque(maxlen=21)
        self.gpu_history = deque(maxlen=21)
        self.core_histories = {}
        self.thermal_stamp = -1000.0
        self.thermal = {}
        self.hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

    def path(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def sensors(self, stamp: float) -> dict:
        if stamp - self.thermal_stamp < 10:
            return self.thermal
        cpu = None
        for zone in self.path("/sys/class/thermal").glob("thermal_zone*"):
            try:
                if read_text(zone / "type", 128).strip() == "mtktscpu":
                    cpu = bounded_number(zone / "temp", 1000, -20, 150)
                    break
            except OSError:
                continue
        battery = self.path("/sys/class/power_supply/battery")
        self.thermal = {"cpu_temperature": cpu,
                        "battery_temperature": bounded_number(battery / "temp", 10, -20, 100),
                        "battery_level": bounded_number(battery / "capacity", 1, 0, 100)}
        self.thermal_stamp = stamp
        return self.thermal

    def processes(self, stamp: float) -> list[dict]:
        rows, current = [], {}
        elapsed = stamp - self.last_stamp if self.last_stamp is not None else 0
        for folder in self.path("/proc").iterdir():
            if not folder.name.isdigit():
                continue
            try:
                name, state, ticks, start, pages = parse_process(read_text(folder / "stat", 8192))
                pid = int(folder.name)
                old = self.old_processes.get((pid, start))
                percent = (ticks - old) / self.hz / elapsed * 100 if old is not None and elapsed > 0 and ticks >= old else None
                rows.append({"pid": pid, "start": start, "name": name, "state": state,
                             "cpu": percent, "rss": pages * self.page_size})
                current[(pid, start)] = ticks
            except (OSError, ValueError, IndexError):
                # /proc entries legitimately vanish while scanning.
                continue
        self.old_processes = current
        return sorted(rows, key=lambda p: (p["cpu"] if p["cpu"] is not None else -1, p["rss"]), reverse=True)

    def ipv4(self) -> str | None:
        if self.root != Path("/"):
            return None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                request = struct.pack("256s", self.interface.encode("ascii")[:15])
                response = fcntl.ioctl(sock.fileno(), 0x8915, request)
                return socket.inet_ntoa(response[20:24])
        except (OSError, UnicodeError):
            return None

    def sample(self) -> dict:
        stamp = time.monotonic()
        result = {"stamp": stamp, "wall_time": time.time(), "errors": [], "cpu": None,
                  "gpu": None, "gpu_source": "/proc/mali/utilization",
                  "cores": [], "processes": [], "rx_rate": None, "tx_rate": None,
                  "memory_total": None, "memory_used": None, "swap_total": None, "swap_used": None,
                  "storage_total": None, "storage_available": None, "uptime": None, "load": None}
        try:
            cpu = parse_cpu(read_text(self.path("/proc/stat")))
            result["cpu"] = cpu_percent(self.old_cpu.get("cpu"), cpu["cpu"])
            for name in sorted((n for n in cpu if n != "cpu"), key=lambda n: int(n[3:])):
                result["cores"].append({"name": name.upper(), "percent": cpu_percent(self.old_cpu.get(name), cpu[name])})
            self.old_cpu = cpu
        except (OSError, ValueError, KeyError) as exc:
            result["errors"].append("CPU: " + type(exc).__name__)
            self.old_cpu = {}
        try:
            result["gpu"] = parse_gpu(read_text(self.path(result["gpu_source"]), 256))
        except (OSError, ValueError) as exc:
            result["errors"].append("GPU: " + type(exc).__name__)
        self.history.append((stamp, result["cpu"]))
        result["history"] = list(self.history)
        self.gpu_history.append((stamp, result["gpu"]))
        result["gpu_history"] = list(self.gpu_history)
        present = {core["name"] for core in result["cores"]}
        self.core_histories = {name: history for name, history in self.core_histories.items() if name in present}
        for core in result["cores"]:
            history = self.core_histories.setdefault(core["name"], deque(maxlen=21))
            history.append((stamp, core["percent"]))
            core["history"] = list(history)
        try:
            result.update(parse_memory(read_text(self.path("/proc/meminfo"))))
        except (OSError, ValueError, IndexError) as exc:
            result["errors"].append("memory: " + type(exc).__name__)
        try:
            result["processes"] = self.processes(stamp)
        except OSError as exc:
            result["errors"].append("processes: " + type(exc).__name__)
        try:
            result["uptime"] = float(read_text(self.path("/proc/uptime"), 128).split()[0])
            result["load"] = read_text(self.path("/proc/loadavg"), 128).split()[:3]
        except (OSError, ValueError, IndexError):
            pass
        try:
            counters = parse_network(read_text(self.path("/proc/net/dev")), self.interface)
            if counters is not None:
                old = self.old_network
                result["rx_rate"] = counter_rate((old[0], old[1]) if old else None, stamp, counters[0])
                result["tx_rate"] = counter_rate((old[0], old[2]) if old else None, stamp, counters[1])
                self.old_network = (stamp, *counters)
            else:
                self.old_network = None
        except (OSError, ValueError, IndexError):
            self.old_network = None
        result["ipv4"] = self.ipv4()
        result["network_online"] = False
        try:
            result["network_online"] = result["ipv4"] is not None and read_text(self.path(f"/sys/class/net/{self.interface}/operstate"), 32).strip() == "up"
        except OSError:
            pass
        try:
            volume = self.path("/run/frl/storage/frl-linux")
            vfs = os.statvfs(volume)
            result["storage_total"] = vfs.f_blocks * vfs.f_frsize
            result["storage_available"] = vfs.f_bavail * vfs.f_frsize
        except OSError:
            pass
        result.update(self.sensors(stamp))
        result["sensor_age"] = stamp - self.thermal_stamp
        self.last_stamp = stamp
        result["sampling_ms"] = (time.monotonic() - stamp) * 1000
        return result


def quantity(value: float | None, rate: bool = False) -> str:
    if value is None:
        return "--"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = 0
    while abs(value) >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    number = f"{value:.2f}" if unit >= 3 else (f"{value:.1f}" if value < 100 else f"{value:.0f}")
    return f"{number} {units[unit]}" + ("/s" if rate else "")


if __name__ == "__main__":
    import json
    reader = Sampler()
    reader.sample()
    time.sleep(1)
    print(json.dumps(reader.sample(), ensure_ascii=False))
