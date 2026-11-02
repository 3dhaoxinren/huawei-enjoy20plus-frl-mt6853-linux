import contextlib,hashlib,importlib.util,io,json,os,signal,subprocess,sys,tarfile,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent))
def load(name,file):
 s=importlib.util.spec_from_file_location(name,Path(__file__).parent/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
backup=load('backup','frl-backup.py');app=load('app','frl-app.py');maintenance=load('maintenance','server-maintenance.py')

class OperationsTests(unittest.TestCase):
 def archive(self,path,name='srv/frl/data/test',kind=None):
  data=b'test data';meta={'files':{name:{'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}}}
  with tarfile.open(path,'w:gz') as t:
   i=tarfile.TarInfo(name);i.size=len(data)
   if kind:i.type=kind;i.linkname='/etc'
   t.addfile(i,io.BytesIO(data));raw=json.dumps(meta).encode();i=tarfile.TarInfo('backup-manifest.json');i.size=len(raw);t.addfile(i,io.BytesIO(raw))
 def test_restore_trusted_directory_roundtrip(self):
  with tempfile.TemporaryDirectory(dir='/root') as d:
   d=Path(d);p=d/'a.tar.gz';self.archive(p);self.assertEqual(backup.restore(p,d/'restored')['verified_files'],1)
   self.assertEqual((d/'restored/srv/frl/data/test').read_bytes(),b'test data')
 def test_writable_parent_and_symlink_rejected_before_create(self):
  with tempfile.TemporaryDirectory(dir='/root') as d:
   d=Path(d);p=d/'a.tar.gz';self.archive(p);parent=d/'writable';parent.mkdir();parent.chmod(0o777)
   with self.assertRaises(ValueError):backup.restore(p,parent/'restored')
   self.assertFalse((parent/'restored').exists());link=d/'alias';link.symlink_to(d,target_is_directory=True)
   with self.assertRaises(OSError):backup.restore(p,link/'restored')
 def test_bad_archive_paths_and_links_rejected(self):
  with tempfile.TemporaryDirectory(dir='/root') as d:
   d=Path(d)
   for n,(name,kind) in enumerate([('/etc/test',None),('srv/frl/data/../../escape',None),('srv/frl/data/link',tarfile.SYMTYPE),('srv/frl/data/hard',tarfile.LNKTYPE)]):
    p=d/(str(n)+'.tar.gz');self.archive(p,name,kind)
    with self.assertRaises(ValueError):backup.restore(p,d/('out'+str(n)))
 def test_recovery_continues_and_records_timeout_or_nonzero(self):
  for failure in [subprocess.TimeoutExpired('sv',6),subprocess.CalledProcessError(1,'sv'),FileNotFoundError('sv')]:
   with tempfile.TemporaryDirectory(dir='/root') as d:
    journal=Path(d)/'journal';journal.write_text(json.dumps({'pending':['first','second']}));calls=[]
    def run(argv,**kw):
     calls.append(argv[-1])
     if argv[-1].endswith('/first'):raise failure
     return Mock(returncode=0)
    original=Path.read_text
    def read(p,*a,**kw):return '{"enabled":true}' if str(p).startswith('/etc/frl/apps/') else original(p,*a,**kw)
    with patch.object(backup,'JOURNAL',journal),patch.object(backup,'locked',lambda p:contextlib.nullcontext()),patch.object(backup,'SHUTDOWN',Path(d)/'stop'),patch.object(Path,'read_text',read),patch.object(backup.subprocess,'run',run):
     result=backup.recover_pending();self.assertEqual(result['pending'],['first']);self.assertEqual(len(calls),2);self.assertEqual(json.loads(journal.read_text())['pending'],['first'])
 def test_shutdown_defers_all_restarts(self):
  with tempfile.TemporaryDirectory(dir='/root') as d:
   d=Path(d);(d/'stop').touch();(d/'journal').write_text('{"pending":["first","second"]}')
   with patch.object(backup,'JOURNAL',d/'journal'),patch.object(backup,'SHUTDOWN',d/'stop'),patch.object(backup,'locked',lambda p:contextlib.nullcontext()),patch.object(backup.subprocess,'run') as run:
    self.assertEqual(backup.recover_pending()['pending'],['first','second']);run.assert_not_called()
 def test_lock_released_on_failure(self):
  with tempfile.TemporaryDirectory(dir='/root') as d:
   p=Path(d)/'lock'
   with self.assertRaises(RuntimeError):
    with backup.locked(p):raise RuntimeError('injected')
   with backup.locked(p):pass
 def test_supervisor_cleanup_continues_after_first_timeout(self):
  with tempfile.TemporaryDirectory(dir='/root') as d:
   d=Path(d);(d/'first').mkdir();(d/'second').mkdir();child=Mock();child.poll.return_value=1;calls=[]
   def run(argv,**kw):
    calls.append(argv[-1])
    if argv[-1].endswith('/first'):raise subprocess.TimeoutExpired('sv',6)
   with patch.object(app,'LIVE',d),patch.object(app,'boot'),patch.object(app.signal,'signal'),patch.object(app.subprocess,'Popen',return_value=child),patch.object(app.subprocess,'run',run):
    with self.assertRaises(RuntimeError):app.supervise()
   self.assertEqual(len(calls),2);child.terminate.assert_called_once();child.wait.assert_called_once()
 def test_maintenance_timeout_requests_term_before_kill(self):
  child=Mock();child.communicate.return_value=('','cancelled');child.poll.return_value=-15;child.returncode=-15
  with patch.object(maintenance.subprocess,'Popen',return_value=child),patch.object(maintenance,'STOP',False):
   r=maintenance.backup_command('create',deadline=-1)
  self.assertTrue(r['cancelled']);child.terminate.assert_called_once();child.kill.assert_not_called()
 def test_maintenance_escalation_preserves_recovery_result(self):
  child=Mock();child.communicate.side_effect=[subprocess.TimeoutExpired('backup',20),('','killed')];child.poll.return_value=-9;child.returncode=-9
  with patch.object(maintenance.subprocess,'Popen',return_value=child),patch.object(maintenance,'STOP',True):r=maintenance.backup_command('create')
  child.terminate.assert_called_once();child.kill.assert_called_once();self.assertEqual(r['returncode'],-9)

identity=load('identity','network_identity.py')
class IdentityTests(unittest.TestCase):
 def test_trusted_local_unicast_mac(self):
  with tempfile.TemporaryDirectory(dir='/root') as d:
   p=Path(d)/'mac';p.write_text('02:12:34:56:78:9a\n');p.chmod(0o600)
   with patch.object(identity,'CONFIG',p):self.assertEqual(identity.read_identity(),'02:12:34:56:78:9a')
 def test_bad_or_writable_config_rejected(self):
  with tempfile.TemporaryDirectory(dir='/root') as d:
   p=Path(d)/'mac';p.write_text('01:12:34:56:78:9a\n');p.chmod(0o600)
   with patch.object(identity,'CONFIG',p),self.assertRaises(ValueError):identity.read_identity()
   p.write_text('02:12:34:56:78:9a\n');p.chmod(0o666)
   with patch.object(identity,'CONFIG',p),self.assertRaises(RuntimeError):identity.read_identity()
 def test_same_identity_does_not_disrupt_interface(self):
  with patch.object(identity,'read_identity',return_value='02:12:34:56:78:9a'),patch.object(identity,'ADDRESS') as address,patch.object(identity.subprocess,'run') as run:
   address.read_text.return_value='02:12:34:56:78:9a\n';identity.apply_identity();run.assert_not_called()
 def test_driver_readback_mismatch_is_not_success(self):
  with patch.object(identity,'read_identity',return_value='02:12:34:56:78:9a'),patch.object(identity,'ADDRESS') as address,patch.object(identity.subprocess,'run'):
   address.read_text.return_value='00:12:34:56:78:9a\n'
   with self.assertRaises(RuntimeError):identity.apply_identity()

import types
with patch.dict(sys.modules,{'vendor_boot':types.ModuleType('vendor_boot')}):health=load('health_feed','health-feed.py')
class TouchHealthTests(unittest.TestCase):
 def run_touch(self,links,changed=False):
  count=[0]
  def read(p,*a,**kw):
   if str(p)=='/run/frl/touch/aptouch.pid':return '123 7'
   if str(p)=='/proc/123/stat':
    count[0]+=1;return '(aptouch) '+' '.join(['0']*19+['8' if changed and count[0]>1 else '7'])
   if str(p)=='/dev/frl-props/odm.thp.dae_alive':return 'true'
   raise AssertionError(str(p))
  with patch.object(Path,'read_text',read),patch.object(Path,'read_bytes',return_value=b'/vendor/bin/aptouch_daemon\0'),patch.object(Path,'iterdir',return_value=iter([Path('/proc/123/fd')/str(i) for i in range(len(links))])),patch.object(health.os,'readlink',side_effect=links):return health.touch_alive()
 def test_closed_unrelated_fd_keeps_required_device_checks(self):
  self.assertTrue(self.run_touch(['/dev/thp',FileNotFoundError('fd closed'),'/dev/input_mt_wrapper']))
 def test_missing_required_device_stays_unhealthy(self):
  self.assertFalse(self.run_touch([FileNotFoundError('thp closed'),'/dev/input_mt_wrapper']))
 def test_pid_identity_changed_during_walk_stays_unhealthy(self):
  self.assertFalse(self.run_touch(['/dev/thp','/dev/input_mt_wrapper'],changed=True))
 def test_permission_error_is_not_ignored(self):
  with self.assertRaises(PermissionError):self.run_touch([PermissionError('denied')])

fuel_health=load('fuel_health','fuel/health.py')
fake_state=Mock();fake_state.encode=lambda x:b'{}'
with patch.dict(sys.modules,{'fuel_runtime':types.ModuleType('fuel_runtime'),'state':fake_state,'health':fuel_health}):fuel_service=load('fuel_service','fuel/service.py')
class FuelEvidenceTests(unittest.TestCase):
 def test_burst_above_old_read_limit_keeps_final_initialization(self):
  record={'pid':42,'initialization':{}}
  lines=[b'6,1,0,-;unrelated log\n']*510+[b'6,2,0,-;[42:fuelgauged] FG_DAEMON_CMD_SET_DAEMON_PID = 42\n',b'6,3,0,-;[42:fuelgauged] FG_DAEMON_CMD_SET_KERNEL_UISOC = 100 gm.ui_soc:100 GM3:0\n',BlockingIOError()]
  with patch.object(fuel_service.os,'read',side_effect=lines),patch.object(fuel_service.time,'monotonic',return_value=0):fuel_service.consume(9,record)
  self.assertTrue(fuel_health.initialized(record))
 def test_gap_invalidates_proof_and_requests_restart(self):
  record={'pid':42,'initialization':{'registered_pid':42,'uisoc_after_registration':True,'gm30_disabled':0}}
  with patch.object(fuel_service.os,'read',side_effect=OSError(32,'log overwritten')),self.assertRaises(fuel_service.LogGap):fuel_service.consume(9,record)
  self.assertFalse(fuel_health.initialized(record));self.assertTrue(record['initialization']['log_gap'])
 def test_foreign_registration_still_invalidates_proof(self):
  record={'pid':42,'initialization':{'registered_pid':42,'uisoc_after_registration':True,'gm30_disabled':0}}
  fuel_service.observe_line(record,'6,3,0,-;FG_DAEMON_CMD_SET_DAEMON_PID = 43')
  self.assertFalse(fuel_health.initialized(record))
 def test_socket_fd_disappears_without_hiding_required_netlink(self):
  with patch.object(Path,'iterdir',return_value=iter([Path('/proc/42/fd/1'),Path('/proc/42/fd/2')])),patch.object(fuel_health.os,'readlink',side_effect=[FileNotFoundError(), 'socket:[88]']),patch.object(Path,'read_text',return_value='header\n0 26 42 0 0 0 0 0 0 88\n'):
   self.assertTrue(fuel_health.socket_ready(42))
 def test_log_gap_terminates_only_owned_child_before_exit(self):
  child=Mock();child.pid=42;child.poll.return_value=None
  with patch.object(fuel_service,'no_existing_daemon'),patch.object(fuel_service,'boot_id',return_value='boot'),patch.object(fuel_service.os,'open',return_value=9),patch.object(fuel_service.os,'lseek'),patch.object(fuel_service.os,'close'),patch.object(fuel_service.subprocess,'run',return_value=Mock(stdout='GNU coreutils')),patch.object(fuel_service.subprocess,'Popen',return_value=child),patch.object(fuel_health,'proc',return_value={'start':7}),patch.object(fuel_service,'consume',side_effect=fuel_service.LogGap('gap')),patch.object(fuel_service,'terminate_owned') as terminate:
   with self.assertRaises(fuel_service.LogGap):fuel_service.run_child()
   terminate.assert_called_once_with(child)

if __name__=='__main__':unittest.main(verbosity=2)
