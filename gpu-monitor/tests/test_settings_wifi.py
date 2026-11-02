import json
import os
from pathlib import Path
import tempfile
import importlib.machinery
import importlib.util
import unittest
from unittest import mock
import display_settings as settings
from wifi_core import WifiError,derive_psk,parse_scan,profile,security_type,unescape
from wifi_worker import connect,request_data
import dashboard
from canvas import Atlas

SCAN=b'bssid / frequency / signal level / flags / ssid\n44:55:66:77:88:99\t2412\t-44\t[WPA2-PSK-CCMP][ESS]\tNewWifi\n'
ORIGINAL=b'ctrl_interface=/run/wpa_supplicant\nupdate_config=0\nnetwork={\n ssid=4f6c6457696669\n psk='+b'1'*64+b'\n key_mgmt=WPA-PSK\n}\n'


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.path=Path(self.folder.name)/'display.json'
    def tearDown(self):self.folder.cleanup()
    def test_default_is_finite(self):self.assertEqual(settings.load_settings(self.path),(60,None))
    def test_every_choice_round_trips(self):
        for seconds,_ in settings.OPTIONS:
            settings.save_settings(seconds,self.path)
            self.assertEqual(settings.load_settings(self.path),(seconds,None))
            self.assertEqual(self.path.stat().st_mode&0o777,0o644)
    def test_bool_negative_arbitrary_rejected(self):
        for seconds in (True,-1,1,61,'60',None):
            with self.assertRaises(ValueError):settings.save_settings(seconds,self.path)
    def test_corruption_never_becomes_never(self):
        for value in (b'{',b'[]',b'{"version":true,"idle_off_seconds":0}',b'{"version":1,"idle_off_seconds":true}',b'x'*1025):
            self.path.write_bytes(value);self.path.chmod(0o644)
            seconds,error=settings.load_settings(self.path)
            self.assertEqual(seconds,60);self.assertIsNotNone(error)
    def test_symlink_read_write_refused(self):
        target=Path(self.folder.name)/'target';target.write_text('{}')
        self.path.symlink_to(target)
        self.assertEqual(settings.load_settings(self.path)[0],60)
        with self.assertRaises(ValueError):settings.save_settings(30,self.path)
        self.assertEqual(target.read_text(),'{}')
    def test_writable_by_others_rejected(self):
        settings.save_settings(0,self.path);self.path.chmod(0o666)
        self.assertEqual(settings.load_settings(self.path)[0],60)


class PowerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path(os.environ.get('FRL_POWER_TEST_PATH',str(Path(__file__).parent/'frl-power-terminal')))
        loader=importlib.machinery.SourceFileLoader('power_settings_test',str(path))
        spec=importlib.util.spec_from_loader(loader.name,loader)
        cls.power=importlib.util.module_from_spec(spec);loader.exec_module(cls.power)
    def test_exact_timeout_boundary(self):
        p=self.power
        with mock.patch.object(p,'load_settings',return_value=(30,None)),mock.patch.object(p,'runtime_mode',return_value='server'),mock.patch.object(p,'command',return_value=mock.Mock(stdout='run: display')):
            with mock.patch.object(p.time,'monotonic',return_value=100):c=p.Console()
            c.visible=True;c.hide=mock.Mock()
            with mock.patch.object(p.time,'monotonic',return_value=129.99):c.tick()
            c.hide.assert_not_called()
            with mock.patch.object(p.time,'monotonic',return_value=130):c.tick()
            c.hide.assert_called_once()
    def test_never_and_hot_reload(self):
        p=self.power
        with mock.patch.object(p,'load_settings',return_value=(0,None)),mock.patch.object(p,'runtime_mode',return_value='server'),mock.patch.object(p,'command',return_value=mock.Mock(stdout='run: display')):
            c=p.Console();c.visible=True;c.activity=p.time.monotonic()-100;c.hide=mock.Mock();c.tick();c.hide.assert_not_called()
            c.settings_checked=p.time.monotonic()-100
            with mock.patch.object(p,'load_settings',return_value=(60,None)):c.tick()
            c.hide.assert_called_once();self.assertEqual(c.idle_off_seconds,60)
class CoreTests(unittest.TestCase):
    def test_scan_deduplicates_and_marks_current(self):
        data=SCAN+b'11:22:33:44:55:66\t5180\t-70\t[WPA2-PSK-CCMP][ESS]\tNewWifi\n'
        rows=parse_scan(data,b'NewWifi'.hex(),{b'NewWifi'.hex()})
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['strength'],-44)
        self.assertTrue(rows[0]['connected'] and rows[0]['saved'])
    def test_security_not_downgraded(self):
        self.assertEqual(security_type('[WPA2-PSK+SAE-CCMP][ESS]'),'psk')
        for flags in ('[WPA2-EAP-CCMP][ESS]','[WEP][ESS]','[WPA2-SAE-CCMP][ESS]','[OWE][ESS]'):
            self.assertEqual(security_type(flags),'unsupported')
        self.assertEqual(security_type('[ESS]'),'open')
    def test_ssid_control_characters_are_data(self):
        row=SCAN.replace(b'NewWifi',b'evil\\x0aSSID\\\\name')
        result=parse_scan(row)[0]
        self.assertNotIn('\n',result['ssid']);self.assertEqual(bytes.fromhex(result['ssid_hex']),b'evil\nSSID\\name')
    def test_psk_known_vector(self):
        self.assertEqual(derive_psk('password',b'IEEE'),'f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e')
    def test_invalid_password_does_not_leak(self):
        with self.assertRaises(WifiError) as context:derive_psk('short',b'NewWifi')
        self.assertNotIn('short',str(context.exception))
    def test_profile_preserves_old_and_hides_plaintext(self):
        new,psk,priority=profile(ORIGINAL,b'NewWifi','secret123','psk')
        self.assertIn(b'ssid=4f6c6457696669',new);self.assertNotIn(b'secret123',new)
        self.assertEqual(len(psk),64);self.assertGreater(priority,0)
        self.assertIn(b'update_config=0',new)
    def test_saved_profile_reused_without_password(self):
        new,psk,_=profile(ORIGINAL,b'OldWifi','','psk')
        self.assertEqual(psk,'1'*64);self.assertEqual(new.count(b'network={'),1)
    def test_open_profile_has_no_psk(self):
        new,psk,_=profile(ORIGINAL,b'Guest','','open')
        self.assertIsNone(psk);self.assertIn(b'key_mgmt=NONE',new)
    def test_malformed_profile_refused(self):
        with self.assertRaises(WifiError):profile(b'network={\n',b'x','12345678','psk')
    def test_request_ssid_injection_refused(self):
        data={'job':'a'*32,'ssid_hex':'00\nSELECT_NETWORK 1','security':'psk','password':'secret123'}
        with self.assertRaises(WifiError):request_data(data)


class FakeControl:
    def __init__(self,auth=True,already=False):self.commands=[];self.selected=False;self.auth=auth;self.already=already
    def request(self,command):
        self.commands.append(command)
        if command=='STATUS':
            if self.already or self.selected and self.auth:return b'ssid=NewWifi\nwpa_state=COMPLETED\nip_address=10.0.0.8'
            if self.selected:return b'wpa_state=SCANNING'
            return b'ssid=OldWifi\nwpa_state=COMPLETED\nip_address=192.168.1.8'
        if command=='SCAN_RESULTS':return SCAN
        if command=='ADD_NETWORK':return b'1'
        if command.startswith('SELECT_NETWORK'):self.selected=True
        if command=='RECONFIGURE':self.selected=False
        return b'OK'


class FakeOps:
    def __init__(self,ready=True,conflict=False):self.clock=0;self.ready=ready;self.conflict=conflict;self.commits=[];self.states=[];self.restarts=0
    def original(self):return ORIGINAL
    def now(self):return self.clock
    def wait(self,seconds):self.clock+=seconds
    def publish(self,phase,message,**kw):self.states.append((phase,message,kw))
    def restart_dhcp(self):self.restarts+=1
    def network_ready(self):return self.ready
    def commit(self,old,new):
        if self.conflict:raise WifiError('配置已变化')
        self.commits.append((old,new))


class TransactionTests(unittest.TestCase):
    def data(self):return {'job':'a'*32,'ssid_hex':b'NewWifi'.hex(),'security':'psk','password':'secret123'}
    def test_success_only_persists_after_network_ready(self):
        ctrl=FakeControl();ops=FakeOps();data=self.data();connect(data,ctrl,ops)
        self.assertEqual(len(ops.commits),1);self.assertEqual(ops.restarts,1)
        self.assertEqual(ops.states[-1][0],'connected');self.assertEqual(data['password'],'')
        self.assertFalse(any('secret123' in cmd for cmd in ctrl.commands))
    def test_current_network_is_no_op(self):
        ctrl=FakeControl(already=True);ops=FakeOps();connect(self.data(),ctrl,ops)
        self.assertEqual(ctrl.commands,['STATUS']);self.assertEqual(ops.commits,[])
    def test_bad_password_restores_without_persisting(self):
        ctrl=FakeControl(auth=False);ops=FakeOps()
        with self.assertRaises(WifiError):connect(self.data(),ctrl,ops)
        self.assertIn('RECONFIGURE',ctrl.commands);self.assertEqual(ops.commits,[]);self.assertLessEqual(ops.clock,7.5)
    def test_dhcp_failure_restores(self):
        ctrl=FakeControl();ops=FakeOps(ready=False)
        with self.assertRaises(WifiError):connect(self.data(),ctrl,ops)
        self.assertIn('RECONFIGURE',ctrl.commands);self.assertEqual(ops.commits,[])
    def test_concurrent_config_edit_not_overwritten(self):
        ctrl=FakeControl();ops=FakeOps(conflict=True)
        with self.assertRaises(WifiError):connect(self.data(),ctrl,ops)
        self.assertEqual(ops.commits,[]);self.assertIn('RECONFIGURE',ctrl.commands)


class FakeTerminal:
    def __init__(self):self.sent=[]
    def send(self,value):self.sent.append(value)


class UiTests(unittest.TestCase):
    def test_password_keys_never_reach_terminal(self):
        terminal=FakeTerminal();ui=dashboard.Dashboard(None,terminal)
        ui.tab='settings';ui.wifi_selected={'ssid':'test'}
        ui.activate(('key','a'));ui.activate(('key','Shift'));ui.activate(('key','1'))
        self.assertEqual(ui.wifi_password,'a!');self.assertEqual(terminal.sent,[])
        ui.activate(('tab','overview'));self.assertEqual(ui.wifi_password,'')
    def test_display_selection_uses_transformed_hit_box(self):
        terminal=FakeTerminal();ui=dashboard.Dashboard(Atlas(),terminal);ui.tab='settings'
        with mock.patch.object(dashboard,'load_settings',return_value=(60,None)),mock.patch.object(dashboard,'save_settings') as save:
            ui.render({})
            box=next(box for box,action in ui.hits if action==('timeout',30))
            self.assertEqual(box[1],332)
            point=(box[0]+10,box[1]+10);self.assertTrue(ui.touch(point,point));save.assert_called_once_with(30)


if __name__=='__main__':unittest.main()
