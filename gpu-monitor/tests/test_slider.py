import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import dashboard
import display_settings as settings
from canvas import Atlas
from touch import Touch, EVENT


class RangeTests(unittest.TestCase):
    def test_full_driver_range(self):
        self.assertEqual([settings.brightness_raw(x) for x in (1,25,50,75,100)], [66,1650,3300,4950,6600])
    def test_fractional_not_five_percent_steps(self):
        self.assertEqual(settings.brightness_raw(42.37),2796)
        self.assertNotEqual(settings.brightness_raw(42.37),settings.brightness_raw(42.38))
    def test_invalid_rejected(self):
        for value in (True,False,0,-1,100.01,'50',None,float('nan'),float('inf')):
            with self.subTest(value=value),self.assertRaises(ValueError):settings.validate_brightness(value)
    def test_roundtrip_fractional_and_old_config(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'brightness.json'
            for value in (1,42.37,100):
                settings.save_brightness(value,p)
                self.assertEqual(settings.load_brightness(p),(value,None))
            p.write_text('{"version":1,"brightness_percent":100}')
            self.assertEqual(settings.load_brightness(p),(100,None))
    def test_corruption_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'b';p.write_text('{"version":1,"brightness_percent":NaN}')
            self.assertIsNotNone(settings.load_brightness(p)[1])
            q=Path(folder)/'link';q.symlink_to(p)
            with self.assertRaises(ValueError):settings.save_brightness(50,q)
            self.assertIsNotNone(settings.load_brightness(q)[1])


class SliderTests(unittest.TestCase):
    def setUp(self):
        self.patches=[mock.patch.object(dashboard,'preview_brightness'),mock.patch.object(dashboard,'clear_brightness_preview'),
                      mock.patch.object(dashboard,'save_brightness'),mock.patch.object(dashboard,'load_brightness',return_value=(100,None))]
        self.preview,self.clear,self.save,self.load=[p.start() for p in self.patches]
        self.ui=dashboard.Dashboard(Atlas(),None);self.ui.tab='settings';self.ui.render({})
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
    def test_track_and_transformed_hitbox(self):
        self.assertEqual(self.ui.slider_box,(28,930,484,64))
        self.assertEqual(self.ui.slider_value((44,962)),1)
        self.assertEqual(self.ui.slider_value((496,962)),100)
    def test_move_previews_release_saves_once(self):
        ui=self.ui;ui.pointer('down',(80,962),(80,962))
        for x in range(100,500,10):ui.pointer('move',(80,962),(x,962))
        self.save.assert_not_called();self.assertGreater(self.preview.call_count,0)
        ui.pointer('up',(80,962),(270,962));self.save.assert_called_once_with(50.5)
        self.clear.assert_called_once();self.assertFalse(ui.slider_drag)
    def test_redraw_does_not_reload_during_drag(self):
        self.ui.pointer('down',(270,962),(270,962));self.ui.render({})
        self.assertEqual(self.ui.brightness_percent,50.5)
    def test_drag_captured_outside_track(self):
        self.ui.pointer('down',(270,962),(270,962))
        self.ui.pointer('up',(270,962),(800,400));self.save.assert_called_once_with(100)
    def test_cancel_reverts_without_saving(self):
        self.ui.pointer('down',(270,962),(270,962));self.ui.pointer('cancel',None,None)
        self.save.assert_not_called();self.clear.assert_called_once();self.assertEqual(self.ui.brightness_percent,100)
    def test_start_outside_does_not_drag(self):
        self.ui.pointer('down',(270,910),(270,910));self.ui.pointer('move',(270,910),(270,962))
        self.preview.assert_not_called();self.assertFalse(self.ui.slider_drag)
    def test_change_tab_cancels(self):
        self.ui.pointer('down',(270,962),(270,962));self.ui.activate(('tab','overview'))
        self.assertFalse(self.ui.slider_drag);self.save.assert_not_called()
    def test_lease_renewal_and_rate_limit(self):
        with mock.patch.object(dashboard.time,'monotonic',return_value=10):
            self.ui.pointer('down',(270,962),(270,962))
            for x in range(100,200):self.ui.pointer('move',(270,962),(x,962))
        self.assertEqual(self.preview.call_count,1)
        with mock.patch.object(dashboard.time,'monotonic',return_value=11.1):self.ui.poll()
        self.assertEqual(self.preview.call_count,2)
    def test_save_error_clears_preview(self):
        self.ui.pointer('down',(270,962),(270,962));self.save.side_effect=OSError('disk')
        with self.assertRaises(OSError):self.ui.pointer('up',(270,962),(300,962))
        self.assertFalse(self.ui.slider_drag);self.clear.assert_called_once()


def decoder():
    t=Touch.__new__(Touch);t.ranges=[(0,1079),(0,2399)];t.type_b=False
    t.slot=0;t.down=False;t.start=None;t.end=False;t.contact=0;t.x=t.y=0
    return t


def packet(*events):
    now=time.monotonic();sec=int(now);usec=int((now-sec)*1000000)
    return b''.join(EVENT.pack(sec,usec,*e) for e in events)


class TouchTests(unittest.TestCase):
    def test_down_move_up_and_legacy(self):
        t=decoder();self.assertEqual(t.feed(packet((1,330,1),(3,53,500),(3,54,1900),(0,0,0))),[])
        self.assertEqual(t.motions[0][0],'down')
        t.feed(packet((3,53,600),(0,0,0)));self.assertEqual(t.motions[0][0],'move')
        legacy=t.feed(packet((1,330,0),(0,0,0)))
        self.assertEqual(t.motions[0][0],'up');self.assertEqual(len(legacy),1)
    def test_drop_cancels_and_does_not_commit(self):
        t=decoder();t.feed(packet((1,330,1),(3,53,500),(3,54,1900),(0,0,0)))
        self.assertEqual(t.feed(packet((0,3,0),(1,330,0),(0,0,0))),[])
        self.assertEqual(t.motions,[('cancel',None,None)])


class PowerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader=importlib.machinery.SourceFileLoader('slider_power',os.environ['FRL_POWER_TEST_PATH'])
        spec=importlib.util.spec_from_loader(loader.name,loader);cls.p=importlib.util.module_from_spec(spec);loader.exec_module(cls.p)
    def console(self):
        with mock.patch.object(self.p,'load_brightness',return_value=(100,None)):c=self.p.Console()
        c.visible=True;c.apply_brightness=mock.Mock();return c
    def test_preview_is_transient_and_fractional(self):
        c=self.console();c.preview(42.37)
        self.assertEqual(c.brightness_percent,100);self.assertEqual(c.preview_percent,42.37)
        c.apply_brightness.assert_called_once_with(42.37)
    def test_preview_refuses_off_and_bad_values(self):
        c=self.console();c.visible=False
        with self.assertRaises(RuntimeError):c.preview(50)
        c.apply_brightness.assert_not_called()
        with self.assertRaises(ValueError):c.preview(float('nan'))
    def test_clear_uses_persisted_final_value(self):
        c=self.console();c.preview(40)
        with mock.patch.object(self.p,'load_brightness',return_value=(63.28,None)):c.clear_preview()
        self.assertIsNone(c.preview_percent);c.apply_brightness.assert_called_with(63.28)
    def test_expired_preview_reverts(self):
        p=self.p;c=self.console();c.preview(40);c.applied_brightness=40;c.preview_until=0
        with mock.patch.object(c,'refresh_settings'),mock.patch.object(p,'runtime_mode',return_value='server'),mock.patch.object(p,'command',return_value=mock.Mock(stdout='run: display')):c.tick()
        self.assertIsNone(c.preview_percent);c.apply_brightness.assert_called_with(100)
    def test_hardware_guard_full_range_not_old_cap(self):
        p=self.p
        with mock.patch.object(p,'LIGHT') as light:
            (light.parent/'max_brightness').read_text.return_value='6600'
            p.check_brightness(6600)
            with self.assertRaises(RuntimeError):p.check_brightness(6601)
            (light.parent/'max_brightness').read_text.return_value='1650'
            with self.assertRaises(RuntimeError):p.check_brightness(6600)


if __name__=='__main__':unittest.main(verbosity=2)
