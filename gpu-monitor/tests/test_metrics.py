import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metrics import Sampler, parse_cpu, cpu_percent, parse_memory, parse_process, parse_network, counter_rate, quantity, bounded_number
from input_keys import key_bytes


class MetricsTests(unittest.TestCase):
    def test_cpu_guest_not_double_counted(self):
        self.assertEqual(parse_cpu("cpu 10 2 3 85 5 2 1 2 9 1\ncpu0 1 0 1 8\n"), {"cpu": (110, 90), "cpu0": (10, 8)})

    def test_cpu_first_and_reset_are_unknown(self):
        self.assertIsNone(cpu_percent(None, (100, 50)))
        self.assertIsNone(cpu_percent((100, 50), (90, 45)))
        self.assertIsNone(cpu_percent((100, 50), (100, 50)))

    def test_cpu_delta(self):
        self.assertAlmostEqual(cpu_percent((100, 50), (200, 125)), 25)

    def test_memory_available_and_swap(self):
        data = parse_memory("MemTotal: 1024 kB\nMemAvailable: 768 kB\nSwapTotal: 16384 kB\nSwapFree: 16380 kB\n")
        self.assertEqual(data["memory_used"], 256 * 1024)
        self.assertEqual(data["swap_used"], 4096)

    def test_missing_available_is_unknown(self):
        self.assertIsNone(parse_memory("MemTotal: 1024 kB\n")["memory_used"])

    def test_process_name_with_parenthesis(self):
        fields = ["S"] + ["0"] * 22
        fields[11], fields[12], fields[19], fields[21] = "10", "20", "1000", "4"
        self.assertEqual(parse_process("12 (a name) with) " + " ".join(fields)), ("a name) with", "S", 30, 1000, 4))

    def test_network_interface(self):
        value = "wlan0: 1024 0 0 0 0 0 0 0 2048 0 0 0 0 0 0 0\n"
        self.assertEqual(parse_network(value, "wlan0"), (1024, 2048))
        self.assertIsNone(parse_network(value, "lo"))

    def test_rate_initial_reset_and_elapsed(self):
        self.assertIsNone(counter_rate(None, 10, 200))
        self.assertIsNone(counter_rate((10, 200), 11, 0))
        self.assertIsNone(counter_rate((10, 200), 10, 300))
        self.assertEqual(counter_rate((10, 200), 12, 400), 100)

    def test_display_units(self):
        self.assertEqual(quantity(None), "--")
        self.assertEqual(quantity(16 * 1024 ** 3), "16.00 GiB")
        self.assertEqual(quantity(1024, True), "1.0 KiB/s")

    def test_temperature_bounds_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "temp"
            self.assertIsNone(bounded_number(path, 1000, -20, 150))
            path.write_text("31800")
            self.assertEqual(bounded_number(path, 1000, -20, 150), 31.8)
            path.write_text("999999")
            self.assertIsNone(bounded_number(path, 1000, -20, 150))

    def test_sample_missing_data_does_not_fake_zeros(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "proc").mkdir()
            sampler = Sampler(d)
            sample = sampler.sample()
            self.assertIsNone(sample["cpu"])
            self.assertIsNone(sample["memory_used"])
            self.assertIsNone(sample["battery_level"])
            self.assertIsNone(sample["storage_available"])
            self.assertFalse(sample["network_online"])
            self.assertGreater(len(sample["errors"]), 0)

    def test_sensor_cache_refreshes_at_ten_seconds(self):
        with tempfile.TemporaryDirectory() as d:
            battery = Path(d, "sys/class/power_supply/battery")
            battery.mkdir(parents=True)
            battery.joinpath("capacity").write_text("76")
            reader = Sampler(d)
            self.assertEqual(reader.sensors(100)["battery_level"], 76)
            battery.joinpath("capacity").write_text("75")
            self.assertEqual(reader.sensors(109)["battery_level"], 76)
            self.assertEqual(reader.sensors(110)["battery_level"], 75)

    def test_disappearing_proc_entry(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "proc/123").mkdir(parents=True)
            self.assertEqual(Sampler(d).processes(100), [])

    def test_pid_reuse_not_spike(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "proc/12")
            path.mkdir(parents=True)
            fields = ["S"] + ["0"] * 22
            fields[11], fields[19], fields[21] = "9000", "2000", "3"
            path.joinpath("stat").write_text("12 (test) " + " ".join(fields))
            reader = Sampler(d)
            reader.old_processes = {(12, 1000): 1}
            reader.last_stamp = 99
            self.assertIsNone(reader.processes(100)[0]["cpu"])

    def test_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "proc").mkdir()
            reader = Sampler(d)
            for _ in range(30):
                reader.sample()
            self.assertEqual(len(reader.history), 21)


class InputTests(unittest.TestCase):
    def test_ascii_and_utf8(self):
        self.assertEqual(key_bytes("a"), b"a")
        self.assertEqual(key_bytes("中"), "中".encode())

    def test_shift(self):
        self.assertEqual(key_bytes("a", shift=True), b"A")
        self.assertEqual(key_bytes("2", shift=True), b"@")

    def test_control(self):
        self.assertEqual(key_bytes("c", ctrl=True), b"\x03")
        self.assertEqual(key_bytes("Ctrl-C"), b"\x03")
        self.assertEqual(key_bytes("d", ctrl=True), b"\x04")

    def test_terminal_special_keys(self):
        self.assertEqual(key_bytes("↑"), b"\x1b[A")
        self.assertEqual(key_bytes("Enter"), b"\r")
        self.assertEqual(key_bytes("⌫"), b"\x7f")
        self.assertEqual(key_bytes("Tab"), b"\t")
        self.assertEqual(key_bytes("unknown"), b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
