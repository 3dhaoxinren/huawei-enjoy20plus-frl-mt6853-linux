import importlib.util,contextlib,json,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent))
import server_common as common
import network_recovery as recovery
import lease as lease_module
spec=importlib.util.spec_from_file_location('server_mode',Path(__file__).with_name('frl-server.py'))
mode_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(mode_module)

class ModeTests(unittest.TestCase):
    def test_missing_mode_preserves_desktop(self):
        with tempfile.TemporaryDirectory() as d,patch.object(common,'MODE',Path(d)/'missing'):
            self.assertEqual(common.runtime_mode(),'desktop')

    def test_invalid_mode_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'mode';common.atomic(path,b'arbitrary\n')
            with patch.object(common,'MODE',path),self.assertRaises(RuntimeError):common.runtime_mode()

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'target';common.atomic(target,b'server\n');link=Path(d)/'link';link.symlink_to(target)
            with patch.object(common,'MODE',link),self.assertRaises(OSError):common.runtime_mode()

    def test_untrusted_permissions_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'mode';common.atomic(path,b'server\n',0o644)
            with patch.object(common,'MODE',path),self.assertRaises(RuntimeError):common.runtime_mode()

    def test_both_modes_readback(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'mode'
            with patch.object(common,'MODE',path):
                for mode in ('server','desktop'):
                    common.atomic(path,(mode+'\n').encode());self.assertEqual(common.runtime_mode(),mode)

class RecoveryTests(unittest.TestCase):
    def test_dead_leader_descendants_are_not_forgotten(self):
        fake=types.SimpleNamespace(Failure=RuntimeError,alive=lambda r:False,group_members=lambda group:[{'pid':19}])
        with patch.dict(sys.modules,{'frl_network':fake}),patch.object(recovery.os,'killpg') as kill:
            with self.assertRaises(RuntimeError):recovery.reap_orphans('dhcp',{'children':[{'pid':3,'group':3}]})
            kill.assert_not_called()

    def test_live_owner_not_reclaimed(self):
        fake=types.SimpleNamespace(Failure=RuntimeError,alive=lambda r:True)
        with patch.dict(sys.modules,{'frl_network':fake}),patch.object(recovery.os,'killpg') as kill:
            with self.assertRaises(RuntimeError):recovery.reap_orphans('wpa',{'children':[{'pid':3}],'owner':{'pid':2}})
            kill.assert_not_called()

    def test_radio_not_blindly_restarted(self):
        fake=types.SimpleNamespace(Failure=RuntimeError,alive=lambda r:r.get('pid')==3)
        with patch.dict(sys.modules,{'frl_network':fake}),patch.object(recovery.os,'killpg') as kill:
            with self.assertRaises(RuntimeError):recovery.reap_orphans('radio',{'children':[{'pid':3}],'owner':{'pid':2}})
            kill.assert_not_called()

    def test_short_disconnect_requests_one_renew_and_waits_for_ack(self):
        generation=[4];connected=[True]
        fake=types.SimpleNamespace(role_alive=lambda _:True,association=lambda:connected[0],RUNTIME=Path('/unused'),
                                   read_json=lambda _: {'generation':generation[0]},managed_ipv4=lambda:True)
        supervisor=types.SimpleNamespace(state={},save=Mock());child=types.SimpleNamespace(pid=17)
        renew=recovery.AssociationRenewal(supervisor,child)
        with patch.dict(sys.modules,{'frl_network':fake}),patch.object(recovery.os,'kill') as kill:
            self.assertTrue(renew());connected[0]=False;self.assertFalse(renew())
            connected[0]=True;self.assertFalse(renew());self.assertFalse(renew());kill.assert_called_once_with(17,recovery.signal.SIGUSR1)
            self.assertTrue(supervisor.state['renew_pending'])
            generation[0]=5;self.assertTrue(renew());self.assertFalse(supervisor.state['renew_pending'])

class RollbackTests(unittest.TestCase):
    def test_desktop_services_restore_before_mode_commit(self):
        calls=[]
        with patch.object(mode_module.SHUTDOWN.__class__,'exists',return_value=False),patch.object(mode_module,'apply',side_effect=lambda v:calls.append(('apply',v))),patch.object(mode_module,'wait_desktop',side_effect=lambda:calls.append(('ready',))),patch.object(mode_module,'atomic',side_effect=lambda *a:calls.append(('mode',a[1]))):
            self.assertEqual(mode_module.rollback('desktop'),[])
        self.assertEqual(calls,[('apply',False),('ready',),('mode',b'desktop\n')])

    def test_failed_server_token_uses_visible_desktop(self):
        with patch.object(Path,'exists',return_value=False),patch.object(mode_module,'atomic',side_effect=OSError('full')),patch.object(mode_module,'runtime_mode',return_value='desktop'),patch.object(mode_module,'apply') as apply,patch.object(mode_module,'wait_desktop'):
            errors=mode_module.rollback('server')
            self.assertTrue(errors);apply.assert_called_once_with(False)

    def test_shutdown_does_not_restart_desktop(self):
        with patch.object(Path,'exists',return_value=True),patch.object(mode_module,'apply') as apply:
            self.assertTrue(mode_module.rollback('desktop'))
            apply.assert_not_called()

class LeaseGenerationTests(unittest.TestCase):
    def exercise(self,fail_first=False):
        env={'interface':'wlan0','ip':'192.0.2.2','subnet':'255.255.255.0','router':'192.0.2.1','dns':'192.0.2.1'}
        memory={'generation':5,'current':lease_module.parse_lease(env),'owned':[lease_module.parse_lease(env)]}
        def load(_):return json.loads(json.dumps(memory))
        def save(_,value):memory.clear();memory.update(value)
        with tempfile.TemporaryDirectory() as d:
            obj=lease_module.LeaseManager.__new__(lease_module.LeaseManager)
            obj.runtime=Path(d);obj.statefile=Path(d)/'lease.json';obj.check_resolver=Mock();obj.check_route_ownership=Mock();obj.route=Mock();obj.drop=Mock(return_value=False);obj.dns_write=Mock()
            obj.command=Mock(side_effect=[lease_module.Failure('injected'),None]) if fail_first else Mock()
            with patch.object(lease_module,'read_json',side_effect=load),patch.object(lease_module,'write_json',side_effect=save):
                if fail_first:
                    with self.assertRaises(lease_module.Failure):obj.apply('renew',env)
                    self.assertEqual(memory['generation'],5)
                else:
                    obj.apply('deconfig',{'interface':'wlan0'});self.assertEqual(memory['generation'],5)
                obj.apply('bound',env);self.assertEqual(memory['generation'],6)

    def test_generation_survives_deconfig(self):self.exercise()
    def test_generation_survives_failed_intent(self):self.exercise(True)

if __name__=='__main__':unittest.main()
