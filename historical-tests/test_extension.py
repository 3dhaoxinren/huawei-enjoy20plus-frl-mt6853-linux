"""Real installed/candidate modules are selected by the external test runner."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import dashboard
import display_settings as settings
from canvas import Atlas, Canvas
from metrics import Sampler, parse_gpu
from scene import draw_overview, draw_compact, gpu_percent


def reading(gpu=13, cl0=0, cl1=0, power=0, frequency=3):
    return f'gpu/cljs0/cljs1={gpu}/{cl0}/{cl1}, frequency index={frequency} power(0:off, 1:0n):{power}\n'


class GpuTests(unittest.TestCase):
    def test_valid_range(self):
        for value in (0, 1, 13, 99, 100):
            self.assertEqual(parse_gpu(reading(value)), value)
    def test_compute_shares_not_added(self):
        self.assertEqual(parse_gpu(reading(30, 45, 55)), 30)
    def test_frequency_and_power_not_used_as_utilization(self):
        self.assertEqual(parse_gpu(reading(13, power=0, frequency=5)), 13)
        self.assertEqual(parse_gpu(reading(0, power=1, frequency=0)), 0)
    def test_invalid_not_zero(self):
        for raw in ('', 'GPU DVFS does not support', reading(101), reading(-1), reading('nan'),
                    reading(13, 101), reading(power=2), reading()+'garbage'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):parse_gpu(raw)
    def test_sample_real_field_and_no_stale_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'proc/mali/utilization';path.parent.mkdir(parents=True)
            reader=Sampler(folder)
            for raw, value in ((reading(13),13),(reading(0),0),('bad',None)):
                path.write_text(raw)
                self.assertEqual(reader.sample()['gpu'],value)
            path.unlink();data=reader.sample()
            self.assertIsNone(data['gpu']);self.assertIn('GPU: FileNotFoundError',data['errors'])
    def test_unknown_display(self):self.assertEqual(gpu_percent(None),'--')
    def test_full_values_fit_both_cards(self):
        class Spy(Canvas):
            def __init__(self):super().__init__(Atlas());self.labels=[]
            def text(self,value,x,y,size,bold=False,width=None,align=None,mono=False):
                advance=sum(self.atlas.glyph(ch,size,bold,mono)[4] for ch in value)
                self.labels.append((value,x,y,x+advance))
                return super().text(value,x,y,size,bold,width,align,mono)
        c=Spy();draw_overview(c,516,968,{'cpu':100,'gpu':100})
        cpu=next(row for row in c.labels if row[0]=='负载 100.0%')
        self.assertLess(cpu[3],382)
        gpu=next(row for row in c.labels if row[0]=='100%')
        self.assertLess(gpu[3],138)
        c=Spy();draw_compact(c,516,183,{'cpu':100,'gpu':100})
        cpu=next(row for row in c.labels if row[0]=='100.0%')
        self.assertLess(cpu[3],138)
        gpu=next(row for row in c.labels if row[0]=='100%')
        self.assertLess(gpu[3],247)


class BrightnessTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.path=Path(self.folder.name)/'brightness.json'
    def tearDown(self):self.folder.cleanup()
    def test_default_preserves_current_brightness(self):
        self.assertEqual(settings.load_brightness(self.path),(100,None))
        self.assertEqual(settings.brightness_raw(100),1650)
    def test_all_steps_persist(self):
        for value in range(5,101,5):
            settings.save_brightness(value,self.path)
            self.assertEqual(settings.load_brightness(self.path),(value,None))
            self.assertEqual(self.path.stat().st_mode&0o777,0o644)
            self.assertTrue(51<=settings.brightness_raw(value)<=1650)
    def test_invalid_range_type_and_step_refused(self):
        for value in (True,False,0,-5,101,110,6,10.0,'60',None):
            with self.subTest(value=value),self.assertRaises(ValueError):settings.save_brightness(value,self.path)
    def test_bad_config_fallback(self):
        for raw in ('{','[]','x'*1025,'{"version":1,"brightness_percent":0}',
                    '{"version":true,"brightness_percent":50}',
                    '{"version":1,"brightness_percent":50,"extra":1}'):
            self.path.write_text(raw);self.path.chmod(0o644)
            value,error=settings.load_brightness(self.path)
            self.assertEqual(value,100);self.assertIsNotNone(error)
    def test_symlink_and_world_writable_refused(self):
        target=self.path.parent/'other';target.write_text('{}');self.path.symlink_to(target)
        self.assertEqual(settings.load_brightness(self.path)[0],100)
        with self.assertRaises(ValueError):settings.save_brightness(20,self.path)
        self.path.unlink();settings.save_brightness(20,self.path);self.path.chmod(0o666)
        self.assertEqual(settings.load_brightness(self.path)[0],100)
    def test_timeout_setting_independent(self):
        other=self.path.parent/'display.json';settings.save_settings(120,other)
        before=other.read_bytes();settings.save_brightness(40,self.path)
        self.assertEqual(before,other.read_bytes())


class UiTests(unittest.TestCase):
    def test_overview_wifi_touch_opens_wifi_and_scans(self):
        ui=dashboard.Dashboard(Atlas(),None)
        model=mock.Mock(scanning=False,busy=False)
        with mock.patch.object(dashboard,'WifiModel',return_value=model):
            ui.render({});self.assertTrue(ui.touch((80,900),(80,900)))
        self.assertEqual((ui.tab,ui.settings_section),('settings','wifi'))
        self.assertGreaterEqual(model.refresh.call_count,1)
    def test_storage_side_does_not_navigate(self):
        ui=dashboard.Dashboard(Atlas(),None);ui.render({})
        self.assertTrue(ui.touch((400,1000),(400,1000)))
        self.assertEqual((ui.tab,ui.detail),('overview','storage'))
    def test_wifi_swipe_does_not_navigate(self):
        ui=dashboard.Dashboard(Atlas(),None);ui.render({})
        self.assertFalse(ui.touch((80,900),(160,900)))
    def test_brightness_hit_is_transformed_and_bounded(self):
        ui=dashboard.Dashboard(Atlas(),None);ui.tab='settings'
        with mock.patch.object(dashboard,'load_brightness',return_value=(100,None)),mock.patch.object(dashboard,'save_brightness') as save:
            ui.render({})
            box=next(box for box,act in ui.hits if act==('brightness',60))
            self.assertEqual(box[1],956)
            point=(box[0]+10,box[1]+10);self.assertTrue(ui.touch(point,point));save.assert_called_once_with(60)
            for box,action in ui.hits:
                if action[0]=='brightness':
                    self.assertTrue(5<=action[1]<=100);self.assertLessEqual(box[1]+box[3],1140)
    def test_minimum_minus_never_turns_off(self):
        ui=dashboard.Dashboard(Atlas(),None);ui.tab='settings'
        with mock.patch.object(dashboard,'load_brightness',return_value=(5,None)):
            ui.render({})
            self.assertEqual(next(action for box,action in ui.hits if box[:2]==(28,882)),('brightness',5))


class PowerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader=importlib.machinery.SourceFileLoader('power_ext_test',os.environ['FRL_POWER_TEST_PATH'])
        spec=importlib.util.spec_from_loader(loader.name,loader);cls.p=importlib.util.module_from_spec(spec);loader.exec_module(cls.p)
    def run_tick(self, visible, raw):
        p=self.p
        with mock.patch.object(p,'load_settings',return_value=(0,None)),mock.patch.object(p,'load_brightness',return_value=(100,None)):
            c=p.Console()
        c.visible=visible
        with mock.patch.object(p,'load_settings',return_value=(0,None)),mock.patch.object(p,'load_brightness',return_value=(40,None)),mock.patch.object(p,'runtime_mode',return_value='server'),mock.patch.object(p,'command',return_value=mock.Mock(stdout='run: display')),mock.patch.object(p,'mode_lock'),mock.patch.object(p,'permitted'),mock.patch.object(p,'LIGHT') as light:
            light.read_text.side_effect=[str(raw),'660']
            c.tick()
            return c,light
    def test_visible_updates_live(self):
        c,light=self.run_tick(True,1650)
        light.write_text.assert_called_once_with('660\n');self.assertEqual(c.applied_brightness,40)
    def test_off_never_wakes(self):
        c,light=self.run_tick(False,0);light.write_text.assert_not_called()
        self.assertEqual(c.brightness_percent,40)
    def test_blanked_not_relit(self):
        c,light=self.run_tick(True,0);light.write_text.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
