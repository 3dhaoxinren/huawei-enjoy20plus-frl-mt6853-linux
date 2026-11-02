import threading
import time
import unittest
from unittest import mock
import wifi_model

HEADER=b'bssid / frequency / signal level / flags / ssid\n'
def scan(name):return HEADER+b'44:55:66:77:88:99\t2412\t-40\t[WPA2-PSK-CCMP][ESS]\t'+name+b'\n'


class FakeControl:
    def __init__(self,cache=True,fail=False,fresh=True,cache_error=False):
        self.gate=threading.Event();self.waiting=threading.Event();self.closed=threading.Event()
        self.cache=cache;self.fail=fail;self.fresh=fresh;self.cache_error=cache_error
        self.finished=False;self.commands=[];self.threads=[]
    def __enter__(self):return self
    def __exit__(self,*args):self.closed.set()
    def request(self,command):
        self.commands.append(command);self.threads.append(threading.get_ident())
        if command=='STATUS':return b'ssid=Cached\nwpa_state=COMPLETED\nip_address=192.168.1.5'
        if command=='LIST_NETWORKS':return b'network id / ssid / bssid / flags\n0\tCached\tany\t[CURRENT]\n'
        if command=='SCAN_RESULTS':
            if self.cache_error and not self.finished:raise OSError('cache error')
            return scan(b'Fresh' if self.finished else b'Cached') if self.cache or self.finished else HEADER
        raise AssertionError(command)
    def scan_wait(self):
        self.waiting.set()
        if not self.gate.wait(3):raise TimeoutError('test gate not released')
        if self.fail:raise OSError('scan failed')
        self.finished=True;return self.fresh


class FastWifiTests(unittest.TestCase):
    def start(self,**kwargs):
        control=FakeControl(**kwargs);model=wifi_model.WifiModel()
        patch=mock.patch.object(wifi_model,'Control',return_value=control);patch.start()
        def cleanup():
            control.gate.set();control.closed.wait(2);patch.stop()
        self.addCleanup(cleanup)
        model.refresh();self.assertTrue(control.waiting.wait(2))
        return model,control
    def finish(self,model,control):
        control.gate.set();deadline=time.monotonic()+2
        while model.scanning and time.monotonic()<deadline:model.poll();time.sleep(.005)
        self.assertFalse(model.scanning)
    def test_cached_list_appears_while_scan_blocked(self):
        model,c=self.start();self.assertTrue(model.poll())
        self.assertEqual(model.networks[0]['ssid'],'Cached');self.assertTrue(model.scanning)
        self.assertIn('上次',model.message);self.assertTrue(model.current['connected'])
    def test_cache_bypasses_connection_poll_throttle(self):
        model,c=self.start();model.next_poll=time.monotonic()+100
        self.assertTrue(model.poll());self.assertTrue(model.networks)
    def test_completion_replaces_cache_and_clears_scanning(self):
        model,c=self.start();model.poll();self.finish(model,c)
        self.assertEqual(model.networks[0]['ssid'],'Fresh');self.assertIn('已找到',model.message)
    def test_empty_cache_still_finishes_fresh_scan(self):
        model,c=self.start(cache=False);model.poll()
        self.assertEqual(model.networks,[]);self.assertTrue(model.current['connected'])
        self.finish(model,c);self.assertEqual(model.networks[0]['ssid'],'Fresh')
    def test_cache_failure_does_not_cancel_fresh_scan(self):
        model,c=self.start(cache_error=True);self.finish(model,c)
        self.assertEqual(model.networks[0]['ssid'],'Fresh')
    def test_refresh_failure_keeps_cache_before_first_poll(self):
        model,c=self.start(fail=True);self.finish(model,c)
        self.assertEqual(model.networks[0]['ssid'],'Cached');self.assertIn('刷新失败',model.message)
    def test_unfinished_scan_is_not_mislabeled_fresh(self):
        model,c=self.start(fresh=False);self.finish(model,c)
        self.assertIn('扫描未完成',model.message)
    def test_all_control_io_off_main_thread(self):
        model,c=self.start();model.poll()
        self.assertNotIn(threading.get_ident(),c.threads)
    def test_repeated_entry_does_not_spawn_duplicate_scan(self):
        model,c=self.start();generation=model._scan_generation
        model.refresh();self.assertEqual(generation,model._scan_generation)
    def test_preserve_connection_message(self):
        model=wifi_model.WifiModel();model.message='连接已完成';model._scan_generation=1
        model._publish_scan(1,[],{'connected':True},'scan text',False,True);model.poll()
        self.assertEqual(model.message,'连接已完成')
    def test_stale_generation_cannot_overwrite_connection(self):
        model,c=self.start();model.poll()
        worker=mock.Mock();worker.stdin=mock.Mock()
        with mock.patch.object(wifi_model,'private_runtime'),mock.patch.object(wifi_model.subprocess,'Popen',return_value=worker):
            model.connect({'ssid_hex':'4e6577','security':'psk'},'fake-password')
        model.current={'connected':True,'ssid':'New'};self.finish(model,c)
        c.closed.wait(1);model.poll()
        self.assertEqual(model.current['ssid'],'New');self.assertIsNone(model._scan_result)
    def test_busy_model_does_not_start_scan(self):
        model=wifi_model.WifiModel();model.busy=True
        with mock.patch.object(wifi_model,'Control') as control:model.refresh()
        control.assert_not_called();self.assertFalse(model.scanning)


if __name__=='__main__':unittest.main(verbosity=2)
