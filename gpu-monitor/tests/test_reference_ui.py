import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from canvas import Atlas, Canvas
import dashboard
from metrics import Sampler
from scene import history_bars, draw_overview, COLORS


class HistoryTests(unittest.TestCase):
    def fixture(self, folder, ticks, gpu):
        p = Path(folder)/'proc';p.mkdir(exist_ok=True)
        (p/'stat').write_text(f'cpu {ticks} 0 0 {ticks}\ncpu0 {ticks} 0 0 {ticks}\n')
        (p/'mali').mkdir(exist_ok=True)
        (p/'mali/utilization').write_text(f'gpu/cljs0/cljs1={gpu}/0/0, frequency index=3 power(0:off, 1:0n):0')
    def test_real_gpu_and_per_core_values(self):
        with tempfile.TemporaryDirectory() as folder:
            self.fixture(folder,100,13);s=Sampler(folder);a=s.sample()
            self.fixture(folder,200,0);b=s.sample()
            self.assertIsNone(a['cores'][0]['percent'])
            self.assertEqual(b['cores'][0]['percent'],50)
            self.assertEqual([v for _,v in b['gpu_history']],[13,0])
            self.assertEqual([v for _,v in b['cores'][0]['history']],[None,50])
    def test_bounded_and_core_hotplug_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            s=Sampler(folder)
            for i in range(30):self.fixture(folder,i+1,0);d=s.sample()
            self.assertEqual(len(d['gpu_history']),21)
            self.assertEqual(len(d['cores'][0]['history']),21)
            (Path(folder)/'proc/stat').write_text('cpu 100 0 0 100\n');s.sample()
            self.assertNotIn('CPU0',s.core_histories)
            self.fixture(folder,200,1);d=s.sample()
            self.assertEqual(len(d['cores'][0]['history']),1)
            self.assertIsNone(d['cores'][0]['percent'])
    def test_failures_do_not_reuse_previous_values(self):
        with tempfile.TemporaryDirectory() as folder:
            self.fixture(folder,100,25);s=Sampler(folder);s.sample()
            (Path(folder)/'proc/mali/utilization').write_text('bad')
            self.assertIsNone(s.sample()['gpu_history'][-1][1])
    def test_graph_zero_unknown_stale_are_not_fake_bars(self):
        c=Canvas(None);history_bars(c,[(0,80),(99,None),(100,0)],10,20,100,50,100)
        self.assertEqual(len(c.vertices)//8,6)  # baseline only
    def test_graph_height_fixed_percent_and_age_position(self):
        c=Canvas(None);history_bars(c,[(70,50),(100,100)],10,20,100,50,100)
        vertices=list(zip(c.vertices[0::8],c.vertices[1::8]))
        self.assertEqual(len(vertices),18)
        self.assertEqual(min(y for x,y in vertices[6:12]),45)
        self.assertEqual(min(y for x,y in vertices[12:]),20)
        self.assertGreater(min(x for x,y in vertices[12:]),min(x for x,y in vertices[6:12]))


class FakeTerminal:
    pane=None;pid=13542;lines=['$ '];cursor=(2,0);scroll=0;history=0
    def resize(self,n):self.rows=n
    def capture(self):pass
    def send(self,value):raise AssertionError('test must not send to terminal')


class UiTests(unittest.TestCase):
    def setUp(self):self.ui=dashboard.Dashboard(Atlas(),FakeTerminal())
    def test_all_eight_cores_with_unknown_placeholders(self):
        labels=[]
        with mock.patch('scene.text',side_effect=lambda c,value,*args,**kw:labels.append(value)):
            draw_overview(Canvas(Atlas()),516,1002,{})
        self.assertEqual([v for v in labels if v.startswith('CPU') and v!='CPU'],['CPU'+str(i) for i in range(8)])
        self.assertIn('--',labels)
    def test_whole_wifi_card_is_shortcut(self):
        for point in ((20,860),(270,900),(520,940)):
            self.ui.tab='overview';self.ui.render({})
            with mock.patch.object(dashboard,'WifiModel',return_value=mock.Mock(scanning=True,busy=False)):
                self.assertTrue(self.ui.touch(point,point))
            self.assertEqual((self.ui.tab,self.ui.settings_section),('settings','wifi'))
    def test_battery_and_system_are_not_wifi_shortcuts(self):
        self.ui.render({});self.ui.touch((80,1000),(80,1000))
        self.assertEqual((self.ui.tab,self.ui.settings_section),('settings','power'))
        self.ui.activate(('tab','overview'));self.ui.render({});self.ui.touch((400,1000),(400,1000))
        self.assertEqual((self.ui.tab,self.ui.detail),('overview','storage'))
    def test_settings_icon_opens_display(self):
        self.ui.render({});self.ui.touch((505,70),(505,70))
        self.assertEqual((self.ui.tab,self.ui.settings_section),('settings','display'))
    def test_bottom_navigation_bounds_and_routes(self):
        for i,name in enumerate(('overview','processes','terminal','settings')):
            self.ui.render({});self.assertTrue(self.ui.touch((i*135+67,1160),(i*135+67,1160)))
            self.assertEqual(self.ui.tab,name)
    def test_full_render_vertex_budget_finite_and_atlas_across_tabs(self):
        now=time.monotonic()
        data={'stamp':now,'cpu':100,'gpu':100,'memory_used':8e9,'memory_total':8e9,
              'swap_used':16e9,'swap_total':16e9,'battery_level':100,'cpu_temperature':100,
              'history':[(now-i*3,100) for i in range(21)],
              'gpu_history':[(now-i*3,100) for i in range(21)],
              'cores':[{'name':'CPU'+str(i),'percent':100,'history':[(now-j*3,100) for j in range(21)]} for i in range(8)],
              'processes':[{'pid':i,'name':'long-process-name-'+str(i),'cpu':800,'rss':1e9} for i in range(36)]}
        for tab in ('overview','processes','terminal','settings','overview'):
            self.ui.tab=tab;c=self.ui.render(data)
            self.assertLess(len(c.vertices)//8,120000)
            self.assertTrue(all(math.isfinite(v) for v in c.vertices))
            self.assertLess(c.atlas.y+c.atlas.row,2048)
    def test_text_contrast(self):
        def luminance(hex):
            rgb=[int(hex[i:i+2],16)/255 for i in (1,3,5)]
            vals=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in rgb]
            return sum(v*k for v,k in zip(vals,(.2126,.7152,.0722)))
        for ink in ('text','muted','blue_text'):
            for bg in ('background','surface','selected'):
                self.assertGreaterEqual((luminance(COLORS[bg])+.05)/(luminance(COLORS[ink])+.05),4.5)


if __name__=='__main__':unittest.main(verbosity=2)
