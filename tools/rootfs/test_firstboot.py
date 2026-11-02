import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import release_firstboot as firstboot


@unittest.skipUnless(os.geteuid() == 0, 'Linux root metadata tests')
class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'etc').mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_real_ssh_keys_and_idempotency(self):
        firstboot.initialize(self.root)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        firstboot.initialize(self.root)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertRegex((self.root/'etc/machine-id').read_text().strip(), '^[0-9a-f]{32}$')
        self.assertEqual(list((self.root/'etc/frl/ssh/authorized_keys').iterdir()), [])
        self.assertNotIn(b'ssid=', (self.root/'etc/wpa_supplicant/frl-wlan0.conf').read_bytes())

    def test_two_devices_different(self):
        firstboot.initialize(self.root)
        with tempfile.TemporaryDirectory() as other:
            firstboot.initialize(Path(other))
            for name in ('etc/machine-id', 'etc/frl/network/wlan-mac', 'etc/ssh/ssh_host_ed25519_key.pub'):
                self.assertNotEqual((self.root/name).read_bytes(), (Path(other)/name).read_bytes())

    def test_empty_machine_id(self):
        (self.root/'etc/machine-id').touch()
        firstboot.initialize(self.root)
        self.assertEqual(len((self.root/'etc/machine-id').read_text().strip()), 32)

    def test_invalid_identity_preserved(self):
        (self.root/'etc/machine-id').write_text('not-an-id')
        with self.assertRaises(RuntimeError):firstboot.initialize(self.root)
        self.assertEqual((self.root/'etc/machine-id').read_text(), 'not-an-id')

    def test_symlink_rejected(self):
        (self.root/'etc/ssh').symlink_to('/tmp')
        with self.assertRaises(RuntimeError):firstboot.initialize(self.root)

    def test_missing_public_key_repaired(self):
        firstboot.initialize(self.root)
        key = self.root/'etc/ssh/ssh_host_ed25519_key'
        before = key.read_bytes()
        key.with_suffix('.pub').unlink()
        firstboot.initialize(self.root)
        self.assertEqual(key.read_bytes(), before)
        self.assertTrue(key.with_suffix('.pub').exists())

    def test_insecure_key_rejected(self):
        firstboot.initialize(self.root)
        (self.root/'etc/ssh/ssh_host_ed25519_key').chmod(0o644)
        with self.assertRaises(RuntimeError):firstboot.initialize(self.root)

    def test_no_native_start_on_build_host(self):
        p = subprocess.run(['python3', '-B', firstboot.__file__], capture_output=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(b'native FRL root required', p.stderr)


if __name__ == '__main__':unittest.main()
