import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import detail_metrics as dm
import management_backend as b
from management_model import ManagementModel
from canvas import Atlas
from dashboard import Dashboard

HASH='a'*64
CONFIG={'schema':1,'enabled':True,'stop_percent':80,'resume_percent':75}
APP={'name':'gui-test','enabled':False,'cwd':'/srv/frl','argv':['/usr/bin/sleep','60']}


class MetricsTests(unittest.TestCase):
    def test_pressure_semantics(self):
        self.assertEqual(dm.pressure('some avg10=1.25 avg60=0\nfull avg10=0.20'),{'some':1.25,'full':.2})
    def test_pressure_bad_values(self):
        for value in ('nan','inf','-1','101'):
            with self.assertRaises(ValueError):dm.pressure('some avg10='+value)
        with self.assertRaises(ValueError):dm.pressure('full avg10=0')
    def test_gpu_does_not_use_cached_frequency(self):
        state=dm.gpu_state('idx: 18, freq: 390000\n(real) freq: 0, freq: 390000\ng_power_count = 0')
        self.assertEqual(state,{'powered':False,'frequency_khz':None})
    def test_gpu_actual_clock(self):
        self.assertEqual(dm.gpu_state('(real) freq: 390000, freq: 500000\ng_power_count = 1')['frequency_khz'],390000)
        with self.assertRaises(ValueError):dm.gpu_state('frequency index=18')
    def test_missing_data_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            d=dm.DetailSampler(root).extra(10,{})
            self.assertIsNone(d['gpu_state']);self.assertIsNone(d['swap_in'])
            self.assertIsNone(d['pressure']['cpu']);self.assertEqual(d['cpu_policies'],[])
    def test_swap_delta_and_counter_reset(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'proc';p.mkdir();vm=p/'vmstat'
            s=dm.DetailSampler(root)
            vm.write_text('pswpin 10\npswpout 20\npgmajfault 5\n');s.extra(10,{})
            vm.write_text('pswpin 14\npswpout 22\npgmajfault 7\n');d=s.extra(12,{})
            self.assertEqual((d['swap_in'],d['swap_out'],d['major_faults']),(2*s.page_size,s.page_size,1))
            vm.write_text('pswpin 0\npswpout 0\npgmajfault 0\n');self.assertIsNone(s.extra(13,{})['swap_in'])
            vm.unlink();s.extra(14,{});self.assertIsNone(s.old_vm)
    def test_memory_history_bounded_and_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            s=dm.DetailSampler(root)
            for i in range(30):d=s.extra(i,{'memory_used':25,'memory_total':100})
            self.assertEqual(len(d['memory_history']),21);self.assertEqual(d['memory_history'][-1][1],25)
            self.assertIsNone(s.extra(31,{})['memory_history'][-1][1])


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='frl-management-test-',dir='/run')
        self.root=Path(self.tmp.name);self.file=self.root/'test.json';self.file.write_text('{}');self.file.chmod(0o600)
    def tearDown(self):self.tmp.cleanup()
    def test_regular_file(self):self.assertEqual(b.trusted_bytes(self.file),b'{}')
    def test_symlink_refused(self):
        link=self.root/'link';link.symlink_to(self.file)
        with self.assertRaises(OSError):b.trusted_bytes(link)
    def test_writable_parent_refused(self):
        self.root.chmod(0o777)
        with self.assertRaises(b.ManagementError):b.trusted_bytes(self.file)
        self.root.chmod(0o700)
    def test_file_mode_and_size_refused(self):
        self.file.chmod(0o666)
        with self.assertRaises(b.ManagementError):b.trusted_bytes(self.file)
        self.file.chmod(0o600);self.file.write_bytes(b'x'*100)
        with self.assertRaises(b.ManagementError):b.trusted_bytes(self.file,16)
    def test_name_and_threshold_validation(self):
        for name in ('../ssh','ssh;reboot','/tmp/a','A','a'*33,None):
            with self.assertRaises(b.ManagementError):b.valid_name(name)
        for values in ((96,75),(80,80),(80,78),(80,39),(True,40),(80.0,75)):
            with self.assertRaises(b.ManagementError):b.validate_thresholds(*values)
        b.validate_thresholds(50,40);b.validate_thresholds(95,92)
    def test_stale_runtime_not_reused(self):
        with mock.patch.object(b,'charge_config',return_value=(CONFIG,HASH)),mock.patch.object(b,'config',return_value=({'monotonic':0},HASH)):
            self.assertIsNone(b.power_snapshot()['runtime'])
    def test_bad_power_values_become_unknown(self):
        value={'monotonic':time.monotonic(),'config':CONFIG,'sample':{'battery_ma':'bad','soc':101,'battery_uv':4130000,'battery_c':float('nan')},'state':'external_power_charge_held'}
        with mock.patch.object(b,'charge_config',return_value=(CONFIG,HASH)),mock.patch.object(b,'config',return_value=(value,HASH)):
            sample=b.power_snapshot()['runtime']['sample']
            self.assertEqual(sample['battery_uv'],4130000)
            for key in ('soc','battery_ma','battery_c'):self.assertIsNone(sample[key])
    def test_job_lock_and_last_id_refuse_duplicate(self):
        runtime=self.root/'jobs'
        with mock.patch.object(b,'RUNTIME',runtime),mock.patch.object(b,'perform',return_value='done') as perform:
            req={'job':'1'*32,'op':'charge'};result=b.run_job(req)
            self.assertEqual(result['phase'],'done');perform.assert_called_once()
            with self.assertRaises(b.ManagementError):b.run_job(req)
            fd=os.open(runtime/'operation.lock',os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:
                with self.assertRaises(BlockingIOError):b.run_job({**req,'job':'2'*32})
            finally:os.close(fd)
            self.assertEqual(b.job_status()['job'],'1'*32)
    def test_job_failure_recorded(self):
        with mock.patch.object(b,'RUNTIME',self.root/'jobs'),mock.patch.object(b,'perform',side_effect=b.ManagementError('failed')):
            r=b.run_job({'job':'1'*32,'op':'app'});self.assertEqual(r['phase'],'failed')


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.req={'job':'1'*32,'op':'charge','expected':HASH,'stop':85,'resume':75}
    def test_stale_charge_never_executes(self):
        with mock.patch.object(b,'charge_config',return_value=(CONFIG,'b'*64)),mock.patch.object(b,'command') as cmd:
            with self.assertRaises(b.ManagementError):b.perform(self.req)
            cmd.assert_not_called()
    def test_charge_preserves_enabled_and_readback(self):
        c={**CONFIG,'enabled':False}
        with mock.patch.object(b,'charge_config',side_effect=[(c,HASH),({**c,'stop_percent':85},HASH)]),mock.patch.object(b,'command') as cmd:
            b.perform(self.req)
            cmd.assert_called_once_with([b.CLI_CHARGE,'configure','--stop','85','--resume','75'])
    def test_charge_readback_mismatch_fails(self):
        with mock.patch.object(b,'charge_config',return_value=(CONFIG,HASH)),mock.patch.object(b,'command'):
            with self.assertRaises(b.ManagementError):b.perform(self.req)
    def test_extra_keys_and_bad_ops_no_command(self):
        with mock.patch.object(b,'command') as cmd:
            for req in ({**self.req,'shell':'reboot'},{**self.req,'op':'shell'},{**self.req,'stop':100}):
                with self.assertRaises(b.ManagementError):b.perform(req)
            cmd.assert_not_called()
    def test_app_enable_requires_matching_config_and_status(self):
        req={'job':'1'*32,'op':'app','expected':HASH,'name':'gui-test','verb':'enable'}
        with mock.patch.object(b,'app_config',side_effect=[(APP,HASH),({**APP,'enabled':True},HASH)]),mock.patch.object(b,'command') as cmd,mock.patch.object(b,'app_snapshot',return_value={'rows':[{'name':'gui-test','running':True}]}):
            b.perform(req);cmd.assert_called_once_with([b.CLI_APP,'enable','gui-test'])
    def test_restart_disabled_refused(self):
        with mock.patch.object(b,'app_config',return_value=(APP,HASH)),mock.patch.object(b,'command') as cmd:
            with self.assertRaises(b.ManagementError):b.perform({'job':'1'*32,'op':'app','expected':HASH,'name':'gui-test','verb':'restart'})
            cmd.assert_not_called()
    def test_core_service_not_registered_refused(self):
        with mock.patch.object(b,'app_config',side_effect=FileNotFoundError),mock.patch.object(b,'command') as cmd:
            with self.assertRaises(FileNotFoundError):b.perform({'job':'1'*32,'op':'app','expected':HASH,'name':'ssh','verb':'disable'})
            cmd.assert_not_called()
    def test_failed_status_not_reported_success(self):
        with mock.patch.object(b,'app_config',side_effect=[(APP,HASH),({**APP,'enabled':True},HASH)]),mock.patch.object(b,'command'),mock.patch.object(b,'app_snapshot',return_value={'rows':[{'name':'gui-test','running':False}]}):
            with self.assertRaises(b.ManagementError):b.perform({'job':'1'*32,'op':'app','expected':HASH,'name':'gui-test','verb':'enable'})


class ModelTests(unittest.TestCase):
    def test_refresh_off_thread_and_single_inflight(self):
        main=threading.get_ident();entered=threading.Event();release=threading.Event();threads=[]
        def read():threads.append(threading.get_ident());entered.set();release.wait(2);return {'rows':[],'stamp':1}
        model=ManagementModel()
        with mock.patch.object(b,'app_snapshot',side_effect=read),mock.patch.object(b,'job_status',return_value=None):
            self.assertTrue(model.refresh('apps'));self.assertTrue(entered.wait(1))
            self.assertFalse(model.refresh('apps'))
            started=time.monotonic();model.poll();self.assertLess(time.monotonic()-started,.05)
            release.set()
            for _ in range(100):
                model.poll()
                if not model.loading:break
                time.sleep(.005)
            self.assertFalse(model.loading);self.assertNotEqual(threads,[main])
    def test_different_app_logs_not_reused(self):
        m=ManagementModel();m._mail=('logs','old',['secret'],None)
        with mock.patch.object(b,'job_status',return_value=None),mock.patch.object(m,'refresh'):
            m.poll('logs','new');self.assertEqual(m.logs,[])
    def test_detached_job_submission(self):
        m=ManagementModel();child=mock.Mock()
        with mock.patch('management_model.subprocess.Popen',return_value=child) as spawn:
            m.submit({'op':'charge','expected':HASH,'stop':80,'resume':75})
            self.assertTrue(spawn.call_args.kwargs['start_new_session']);self.assertTrue(m.busy)
            self.assertEqual(json.loads(child.stdin.write.call_args.args[0])['job'],m.job)
            with self.assertRaises(b.ManagementError):m.submit({})
    def test_unknown_result_never_retries(self):
        m=ManagementModel();m.started=0;m.job='1'*32;m.worker=mock.Mock();m.worker.poll.return_value=1;m.busy=True
        with mock.patch.object(b,'job_status',return_value=None),mock.patch.object(m,'submit') as submit:
            m.poll();self.assertFalse(m.busy);submit.assert_not_called()


class UiTests(unittest.TestCase):
    def setUp(self):
        self.ui=Dashboard(Atlas(),None)
        self.ui.management.power={'config':dict(CONFIG),'sha256':HASH,'runtime':None,'stamp':time.monotonic()}
        self.ui.management.apps={'rows':[{'name':'gui-test','sha256':HASH,'enabled':False,'running':False,'status':'down'}],'stamp':time.monotonic()}
    def test_overview_detail_routes(self):
        for point,kind in [((100,200),'memory'),((100,350),'gpu'),((400,500),'cpu'),((100,750),'cpu'),((400,1000),'storage')]:
            self.ui.tab='overview';self.ui.detail=None;self.ui.render({});self.assertTrue(self.ui.touch(point,point));self.assertEqual(self.ui.detail,kind)
    def test_battery_opens_power_without_writing(self):
        self.ui.render({})
        with mock.patch.object(self.ui.management,'submit') as submit:
            self.ui.touch((100,1000),(100,1000));self.assertEqual(self.ui.settings_section,'power');submit.assert_not_called()
    def test_charge_confirmation_cancel_and_cooldown(self):
        self.ui.tab='settings';self.ui.settings_section='power';self.ui.render({})
        with mock.patch.object(self.ui.management,'submit') as submit:
            self.ui.activate(('m_charge_confirm',None));self.ui.render({})
            self.assertTrue(all(a[0] in ('m_cancel','m_execute') for box,a in self.ui.hits))
            with self.assertRaises(b.ManagementError):self.ui.activate(('m_execute',None))
            self.ui.activate(('m_cancel',None));submit.assert_not_called()
    def test_charge_confirmation_uses_snapshot(self):
        self.ui.tab='settings';self.ui.settings_section='power';self.ui.render({});self.ui.activate(('m_threshold',(0,1)))
        self.ui.activate(('m_charge_confirm',None));self.ui.management_pending['ready_at']=0
        with mock.patch.object(self.ui.management,'submit') as submit:
            self.ui.activate(('m_execute',None));self.assertEqual(submit.call_args.args[0]['stop'],81)
    def test_stale_config_requires_refresh(self):
        self.ui.tab='settings';self.ui.settings_section='power';self.ui.render({});self.ui.management.power['sha256']='b'*64
        with self.assertRaises(b.ManagementError):self.ui.activate(('m_charge_confirm',None))
    def test_expired_power_snapshot_cannot_save(self):
        self.ui.management.power['stamp']=time.monotonic()-100
        self.ui.tab='settings';self.ui.settings_section='power';self.ui.render({})
        self.assertFalse(any(a[0]=='m_charge_confirm' for box,a in self.ui.hits))
        with self.assertRaises(b.ManagementError):self.ui.activate(('m_charge_confirm',None))
    def test_expired_service_snapshot_cannot_operate(self):
        self.ui.service_selected='gui-test';self.ui.management.apps['stamp']=time.monotonic()-100
        with self.assertRaises(b.ManagementError):self.ui.activate(('m_app_confirm','enable'))
    def test_services_excludes_core_controls(self):
        self.ui.tab='processes';self.ui.service_view=True;self.ui.render({})
        self.assertEqual([a for box,a in self.ui.hits if a[0]=='m_select_app'],[('m_select_app','gui-test')])
    def test_all_pages_gpu_budget_and_atlas(self):
        d=dm.DetailSampler().sample();d['render_frames']=123;d['render_ms']=18
        for kind in ('cpu','gpu','memory','storage'):
            self.ui.detail=kind;c=self.ui.render(d);self.assertLess(len(c.vertices)//8,120000)
        self.ui.detail=None;self.ui.tab='settings';self.ui.settings_section='power';self.ui.render(d)
        self.ui.tab='processes';self.ui.service_view=True;self.ui.render(d)
        self.ui.service_selected='gui-test';self.ui.render(d);self.ui.show_logs=True;self.ui.management.logs=['app log']*60;self.ui.render(d)
        self.assertLess(self.ui.atlas.y+self.ui.atlas.row,2048)


if __name__=='__main__':unittest.main(verbosity=2)
