import ast
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

import collect_frl as c


def archive_bytes(items):
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w:gz') as t:
        for name,data,kind,target in items:
            m=tarfile.TarInfo(name);m.mode=0o644
            if kind:m.type=kind;m.linkname=target
            else:m.size=len(data)
            t.addfile(m,None if kind else io.BytesIO(data))
    return out.getvalue()


class ValidationTests(unittest.TestCase):
    def test_remote_script_compiles_and_has_no_image_copy(self):
        ast.parse(c.remote_source())
        self.assertNotIn('dd ',c.REMOTE)
        self.assertNotIn('fsfreeze',c.REMOTE)
        self.assertIn("'--one-file-system'",c.REMOTE)
        self.assertIn("b'mediatek,MT6853\\0'",c.REMOTE)
        self.assertIn("+'\\n'",c.REMOTE)

    def test_ssh_is_noninteractive_and_strict(self):
        args=c.ssh_command('192.0.2.10',ssh='ssh')
        self.assertIn('StrictHostKeyChecking=yes',args)
        self.assertIn('BatchMode=yes',args)
        self.assertEqual(args[-2:],["frl@192.0.2.10",'sudo -n /usr/bin/python3 -B -'])

    def test_ssh_rejects_shell_options_and_injection(self):
        for host in ('-oProxyCommand=x','host;echo hi','x\ny','user@host','x $(id)',''):
            with self.subTest(host=host),self.assertRaises(ValueError):c.ssh_command(host,ssh='ssh')
        for user in ('root;id','-x',''):
            with self.assertRaises(ValueError):c.ssh_command('localhost',user=user,ssh='ssh')
        for port in (0,65536):
            with self.assertRaises(ValueError):c.ssh_command('localhost',port=port,ssh='ssh')

    def test_key_path_is_argument_not_shell_code(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'key & space';p.write_text('fixture')
            args=c.ssh_command('host',identity=p,ssh='ssh')
            self.assertEqual(args[args.index('-i')+1],str(p.resolve()))

    def test_known_private_patterns(self):
        for name in ('etc/shadow','etc/ssh/ssh_host_ed25519_key','etc/frl/ssh/authorized_keys/frl',
                     'etc/wpa_supplicant/frl-wlan0.conf','var/log/a','etc/frl/network/wlan-mac'):
            self.assertTrue(c.excluded(name),name)
        self.assertFalse(c.excluded('etc/frl/sshd_config'))

    def test_metadata_parse_and_duplicate_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'log';line=c.PREFIX+json.dumps({'phase':'before'})+'\n'
            p.write_text('ordinary diagnostic\n'+line)
            self.assertIn('before',c.reports(p))
            p.write_text(line+line)
            with self.assertRaises(ValueError):c.reports(p)


class ArchiveTests(unittest.TestCase):
    def verify(self,items,expected):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.tar.gz';p.write_bytes(archive_bytes(items))
            return c.verify_archive(p,expected)

    def test_exact_payload_and_usrmerge(self):
        raw=b'payload';want=hashlib.sha256(raw).hexdigest()
        result=self.verify([('usr/sbin/frl-init',raw,None,None),('sbin',b'',tarfile.SYMTYPE,'usr/sbin')],{'sbin/frl-init':want})
        self.assertEqual(result['payload_files_verified'],1)

    def test_hardlink_to_untracked_regular_content(self):
        raw=b'same';want=hashlib.sha256(raw).hexdigest()
        result=self.verify([('usr/a',raw,None,None),('usr/b',b'',tarfile.LNKTYPE,'usr/a')],{'usr/b':want})
        self.assertTrue(result['gzip_and_tar_verified'])

    def test_missing_or_wrong_payload_refused(self):
        for expected in ({'usr/b':'a'*64},{'usr/a':'b'*64}):
            with self.assertRaises(ValueError):self.verify([('usr/a',b'x',None,None)],expected)

    def test_private_and_unsafe_paths_refused(self):
        for name in ('etc/shadow','home/frl/a','../outside','/etc/passwd','etc/../x','etc\\x'):
            with self.subTest(name=name),self.assertRaises(ValueError):self.verify([(name,b'x',None,None)],{})

    def test_duplicate_paths_and_special_files_refused(self):
        with self.assertRaises(ValueError):self.verify([('usr/a',b'x',None,None)]*2,{})
        with self.assertRaises(ValueError):self.verify([('usr/pipe',b'',tarfile.FIFOTYPE,'')],{})

    def test_truncation_is_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.gz';p.write_bytes(archive_bytes([('usr/a',b'x',None,None)])[:-5])
            with self.assertRaises((EOFError,tarfile.TarError,OSError)):c.verify_archive(p,{})

    def test_cancel_refuses_completion(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.gz';p.write_bytes(archive_bytes([('usr/a',b'x',None,None)]))
            event=threading.Event();event.set()
            with self.assertRaises(RuntimeError):c.verify_archive(p,{},cancel=event)


class ClientFlowTests(unittest.TestCase):
    def scenario(self, check=False, missing_after=False, fail=False):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);raw=b'test';payload={'usr/lib/frl/test':hashlib.sha256(raw).hexdigest()}
            archive=archive_bytes([('usr/lib/frl/test',raw,None,None)])
            state={'payload':payload,'payload_manifest_sha256':'a'*64,'dpkg_status_sha256':'b'*64}
            before=c.PREFIX+json.dumps({'phase':'before','state':state})+'\n'
            after=c.PREFIX+json.dumps({'phase':'after','state':state})+'\n'
            helper=root/'fake_ssh.py'
            helper.write_text('import sys,base64\nsys.stdin.buffer.read()\n'
                              +'sys.stderr.write('+repr(before if check or missing_after else before+after)+')\n'
                              +'sys.stdout.buffer.write(base64.b64decode('+repr(base64.b64encode(b'' if check else archive).decode())+'))\n'
                              +'sys.exit('+str(1 if fail else 0)+')\n')
            free=mock.Mock(free=20*1024**3)
            with mock.patch.object(c,'ssh_command',return_value=[sys.executable,str(helper)]),mock.patch.object(c.shutil,'disk_usage',return_value=free):
                if missing_after or fail:
                    with self.assertRaises(RuntimeError):c.collect('fixture',root/'out',check_only=check)
                    folder=next((root/'out').iterdir())
                else:folder=c.collect('fixture',root/'out',check_only=check)
            result=json.loads((folder/'result.json').read_text())
            if missing_after or fail:
                self.assertFalse(result['complete']);self.assertTrue((folder/'system-files.private.tar.gz.partial').exists())
            else:
                self.assertTrue(result['complete'])
                self.assertEqual((folder/'system-files.private.tar.gz').exists(),not check)
                self.assertTrue((folder/'device-info.private.json').exists())

    def test_successful_local_binary_fixture(self):self.scenario()
    def test_connection_check_does_not_claim_image_export(self):self.scenario(check=True)
    def test_missing_after_report_fails(self):self.scenario(missing_after=True)
    def test_nonzero_transport_fails(self):self.scenario(fail=True)


@unittest.skipUnless(sys.platform.startswith('linux'), 'GNU tar integration runs on Linux')
class GnuTarTests(unittest.TestCase):
    def test_actual_tar_options_metadata_and_exclusions(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'root';root.mkdir()
            for name in ('etc/ssh','etc/frl/ssh/authorized_keys','etc/wpa_supplicant','usr/lib/frl','var/log'):
                (root/name).mkdir(parents=True,exist_ok=True)
            private=['etc/shadow','etc/ssh/ssh_host_ed25519_key','etc/frl/ssh/authorized_keys/frl','etc/wpa_supplicant/frl-wlan0.conf','var/log/test']
            for name in private:(root/name).write_bytes(b'PRIVATE_FIXTURE')
            payload=root/'usr/lib/frl/example';payload.write_bytes(b'actual payload');payload.chmod(0o755)
            image=Path(d)/'actual.tar.gz'
            command=['tar','--one-file-system','--numeric-owner','--acls','--xattrs','--xattrs-include=*','--sparse','--format=pax','-C',str(root),'-czf',str(image)]
            command+=['--exclude='+p for p in c.EXCLUDES]+['etc','usr','var']
            subprocess.run(command,check=True,capture_output=True)
            result=c.verify_archive(image,{'usr/lib/frl/example':c.digest(payload)})
            self.assertEqual(result['payload_files_verified'],1)
            with tarfile.open(image) as t:
                self.assertEqual(t.getmember('usr/lib/frl/example').mode,0o755)
                self.assertFalse(any(c.excluded(m.name) for m in t))


if __name__=='__main__':unittest.main(verbosity=2)
