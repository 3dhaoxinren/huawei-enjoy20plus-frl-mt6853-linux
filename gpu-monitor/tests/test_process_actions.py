import errno
import os
import signal
import tempfile
import time
from pathlib import Path
import unittest
from unittest import mock

from canvas import Atlas
import dashboard
import process_actions as actions


def info(**kw):
    result={'pid':765432,'start':100,'uid':1000,'name':'demo-worker','state':'S','ppid':1,
            'flags':0,'exe':'/usr/bin/sleep','cmd_hash':'abc'}
    result.update(kw);return result


class ClassificationTests(unittest.TestCase):
    def test_positive_pid_only(self):
        for pid in (0,-1,-10,True,'10',None):
            with self.assertRaises(actions.ProcessError):actions.read_process(pid)
    def test_parse_stat_handles_parentheses(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'123';p.mkdir()
            fields=['S','1','0','0','0','0','0']+['0']*12+['456']+['0']*4
            (p/'stat').write_text('123 (a name) x) '+' '.join(fields))
            (p/'cmdline').write_bytes(b'/usr/bin/sleep\0secret-argument\0')
            with mock.patch.object(actions,'PROC',root):value=actions.read_process(123)
            self.assertEqual(value['name'],'a name) x');self.assertEqual(value['start'],456)
            self.assertNotIn('secret',str(value));self.assertEqual(len(value['cmd_hash']),64)
    def test_core_processes_protected(self):
        with mock.patch.object(actions,'service_of',return_value=None):
            for value in (info(pid=1),info(pid=2),info(pid=os.getpid()),info(flags=0x200000),info(name='runsv'),info(name='init')):
                self.assertEqual(actions.classify(value)[0],'protected')
    def test_hardware_and_display_protected(self):
        for service in actions.PROTECTED_SERVICES:
            with mock.patch.object(actions,'service_of',return_value=service):
                self.assertEqual(actions.classify(info())[0],'protected')
    def test_network_services_have_specific_impact(self):
        for service,word in (('ssh','SSH'),('wpa','Wi-Fi'),('dhcp','IP')):
            with mock.patch.object(actions,'service_of',return_value=service):
                risk,lines=actions.classify(info());self.assertEqual(risk,'important');self.assertIn(word,' '.join(lines))
    def test_unknown_root_and_shell_need_two_confirmations(self):
        with mock.patch.object(actions,'service_of',return_value=None):
            self.assertEqual(actions.classify(info(uid=0))[0],'important')
            self.assertEqual(actions.classify(info(name='bash'))[0],'important')
            self.assertEqual(actions.classify(info())[0],'normal')
    def test_service_ancestor_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);pid=root/'wpa/supervise/pid';pid.parent.mkdir(parents=True);pid.write_text('100')
            with mock.patch.object(actions,'SERVICES',root),mock.patch.object(actions,'read_process',return_value=info(pid=100)):
                self.assertEqual(actions.service_of(info(ppid=100)),'wpa')
    def test_parent_loop_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(actions,'SERVICES',Path(directory)),mock.patch.object(actions,'read_process',return_value=info(ppid=765432)) as read:
                self.assertIsNone(actions.service_of(info(ppid=765432)));self.assertEqual(read.call_count,1)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.target=info();self.clock=10
        self.patches=[mock.patch.object(actions,'read_process',side_effect=lambda pid:dict(self.target)),
                      mock.patch.object(actions,'classify',return_value=('normal',['impact'])),
                      mock.patch.object(actions.time,'monotonic',side_effect=lambda:self.clock),
                      mock.patch.object(actions.os,'pidfd_open',side_effect=OSError(errno.ENOSYS,'unsupported')),
                      mock.patch.object(actions.os,'kill')]
        self.read,self.classify,self.monotonic,self.pidfd,self.kill=[p.start() for p in self.patches]
        self.control=actions.ProcessActions()
    def tearDown(self):
        for patch in reversed(self.patches):patch.stop()
    def select(self):self.control.select({'pid':765432,'start':100})
    def test_select_never_sends_signal(self):
        self.select();self.kill.assert_not_called();self.assertEqual(self.control.pending['stage'],'confirm')
    def test_stale_list_rejected(self):
        with self.assertRaises(actions.ProcessError):self.control.select({'pid':765432,'start':99})
        self.kill.assert_not_called()
    def test_cooldown_prevents_double_tap(self):
        self.select()
        with self.assertRaises(actions.ProcessError):self.control.terminate()
        self.kill.assert_not_called()
    def test_ordinary_one_confirmation_sends_only_sigterm(self):
        self.select();self.clock+=1;self.control.terminate()
        self.kill.assert_called_once_with(765432,signal.SIGTERM)
        self.assertEqual(self.control.last_result['outcome'],'signal_sent')
    def test_important_requires_explicit_second_stage(self):
        self.classify.return_value=('important',['impact']);self.select();self.clock+=1
        with self.assertRaises(actions.ProcessError):self.control.terminate()
        self.control.advance();self.assertEqual(self.control.pending['stage'],'confirm')
        with self.assertRaises(actions.ProcessError):self.control.terminate()
        self.clock+=1;self.control.terminate();self.kill.assert_called_once()
    def test_protected_cannot_advance_or_signal(self):
        self.classify.return_value=('protected',['core']);self.select();self.clock+=10
        with self.assertRaises(actions.ProcessError):self.control.advance()
        with self.assertRaises(actions.ProcessError):self.control.terminate()
        self.kill.assert_not_called()
    def test_cancel_invalidates_confirmation(self):
        self.select();self.control.cancel();self.clock+=1
        with self.assertRaises(actions.ProcessError):self.control.terminate()
        self.kill.assert_not_called()
    def test_reused_pid_refused(self):
        self.select();self.target['start']=101;self.clock+=1;self.control.terminate()
        self.kill.assert_not_called();self.assertEqual(self.control.last_result['outcome'],'not_sent')
    def test_exec_or_identity_change_refused(self):
        for field,value in (('exe','/bin/other'),('uid',0),('cmd_hash','changed'),('name','changed')):
            self.target=info();self.select();self.target[field]=value;self.clock+=1;self.control.terminate()
        self.kill.assert_not_called()
    def test_newly_protected_target_refused(self):
        self.select();self.clock+=1;self.classify.return_value=('protected',['core']);self.control.terminate()
        self.kill.assert_not_called()
    def test_second_validation_catches_reuse(self):
        self.select();self.clock+=1
        self.read.side_effect=[info(),info(start=101)]
        self.control.terminate();self.kill.assert_not_called()
    def test_pidfd_path_used_when_supported(self):
        self.select();self.clock+=1;self.pidfd.side_effect=None;self.pidfd.return_value=123
        with mock.patch.object(actions.signal,'pidfd_send_signal') as send,mock.patch.object(actions.os,'close') as close:
            self.control.terminate();send.assert_called_once_with(123,signal.SIGTERM);close.assert_called_once_with(123)
        self.kill.assert_not_called()
    def test_pidfd_permission_error_does_not_fallback(self):
        self.select();self.clock+=1;self.pidfd.side_effect=PermissionError(errno.EPERM,'denied')
        self.control.terminate();self.kill.assert_not_called()
    def test_no_auto_sigkill_if_still_running(self):
        self.select();self.clock+=1;self.control.terminate();self.clock+=4
        self.assertTrue(self.control.poll());self.assertEqual(self.control.last_result['outcome'],'still_running')
        self.kill.assert_called_once_with(765432,signal.SIGTERM)
    def test_exit_and_reused_pid_are_not_signalled_again(self):
        self.select();self.clock+=1;self.control.terminate();self.target['start']=101
        self.assertTrue(self.control.poll());self.assertEqual(self.control.last_result['outcome'],'exited')
        self.kill.assert_called_once()


class UiTests(unittest.TestCase):
    def setUp(self):self.ui=dashboard.Dashboard(Atlas(),None)
    def test_process_row_touch_target_and_snapshot(self):
        self.ui.tab='processes';self.ui.render({'processes':[{'pid':123,'start':456,'name':'sleep','cpu':0,'rss':0}]})
        box,action=next((b,a) for b,a in self.ui.hits if a[0]=='process')
        self.assertEqual(box[3],44);self.assertEqual(action[1],{'pid':123,'start':456})
    def test_overview_process_summary_opens_full_list(self):
        self.ui.render({});self.assertTrue(self.ui.touch((100,500),(100,500)));self.assertEqual(self.ui.tab,'processes')
    def test_modal_blocks_background_and_navigation(self):
        self.ui.process_actions.pending={'target':info(),'risk':'important','impact':['Network interruption'],
                                         'stage':'warning','ready_at':0}
        self.ui.render({});self.assertFalse(self.ui.touch((337,1160),(337,1160)))
        self.assertTrue(all(action[0].startswith('process_') for box,action in self.ui.hits))
    def test_cooldown_has_no_live_danger_hit(self):
        self.ui.process_actions.pending={'target':info(),'risk':'important','impact':[],
                                         'stage':'confirm','ready_at':time.monotonic()+5}
        self.ui.render({});self.assertFalse(any(a[0]=='process_end' for b,a in self.ui.hits))
    def test_gpu_zero_displays_frame_count_not_fake_percent(self):
        from canvas import Canvas
        from scene import draw_overview
        labels=[]
        class Spy(Canvas):
            def text(self,value,*args,**kw):labels.append(str(value));return super().text(value,*args,**kw)
        draw_overview(Spy(Atlas()),516,1002,{'gpu':0,'render_frames':123})
        self.assertIn('0%',labels);self.assertTrue(any('123 帧' in text for text in labels))
        self.assertTrue(any('静态页面' in text for text in labels))


if __name__=='__main__':unittest.main(verbosity=2)
