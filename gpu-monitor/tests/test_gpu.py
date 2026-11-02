"""CPU-side protocol/layout/input regression tests; actual raster tests run on Mali."""
import sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'gpu'),str(Path(__file__).resolve().parents[1]/'src')]
import time
import unittest
from canvas import Atlas, Canvas
from dashboard import Dashboard
from touch import Touch, EVENT
from terminal import spans, cells
from scene import draw_overview


class FakeTerminal:
    def __init__(self):self.sent=[];self.history=100;self.scroll=0
    def send(self,data):self.sent.append(data)
    def capture(self):pass


def decoder(type_b=False):
    t=Touch.__new__(Touch)
    t.ranges=[(0,1079),(0,2399)];t.type_b=type_b
    t.slot=0;t.down=False;t.start=None;t.end=False;t.contact=0;t.x=t.y=0
    return t


def events(*triples):
    now=time.monotonic();sec=int(now);us=int((now-sec)*1e6)
    return b''.join(EVENT.pack(sec,us,*e) for e in triples)


class InputTests(unittest.TestCase):
    def test_type_a_release(self):
        t=decoder()
        self.assertEqual(t.feed(events((1,330,1),(3,57,0),(3,53,500),(3,54,1000),(0,2,0),(0,0,0))),[])
        start=t.start
        t.feed(events((3,57,0),(3,53,502),(3,54,1001),(0,2,0),(0,0,0)))
        self.assertEqual(start,t.start)
        result=t.feed(events((1,330,0),(0,2,0),(0,0,0)))
        self.assertEqual(len(result),1);self.assertEqual(result[0][0],start)
    def test_type_b(self):
        t=decoder(True)
        t.feed(events((3,47,0),(3,57,7),(3,53,100),(3,54,200),(0,0,0)))
        self.assertEqual(len(t.feed(events((3,57,-1),(0,0,0)))),1)
    def test_extra_contact_does_not_move_primary(self):
        t=decoder()
        t.feed(events((1,330,1),(3,57,0),(3,53,100),(3,54,200),(0,2,0),
                      (3,57,1),(3,53,900),(3,54,2000),(0,2,0),(0,0,0)))
        self.assertLess(t.x,100)
    def test_dropped_packet_cancels(self):
        t=decoder();t.down=True;t.start=(10,10)
        self.assertEqual(t.feed(events((0,3,0),(1,330,0),(0,0,0))),[])
    def test_keyboard_ctrl_shift_and_symbols(self):
        terminal=FakeTerminal();ui=Dashboard(None,terminal)
        ui.activate(('key','Ctrl'));ui.activate(('key','c'))
        ui.activate(('key','Shift'));ui.activate(('key','1'))
        ui.activate(('key','←'));ui.activate(('key','Enter'))
        self.assertEqual(terminal.sent,[b'\x03',b'!',b'\x1b[D',b'\r'])
        self.assertFalse(ui.ctrl or ui.shift)
    def test_swipe_never_activates_key(self):
        terminal=FakeTerminal();ui=Dashboard(None,terminal)
        ui.hits=[((0,0,100,100),('key','x'))]
        self.assertFalse(ui.touch((10,10),(80,80)))
        self.assertEqual(terminal.sent,[])
    def test_cross_boundary_tap_cancelled(self):
        terminal=FakeTerminal();ui=Dashboard(None,terminal)
        ui.hits=[((0,0,30,30),('key','x'))]
        self.assertFalse(ui.touch((28,20),(32,20)))


class SceneTests(unittest.TestCase):
    def test_glyph_cached_not_frame_rasterized(self):
        a=Atlas();one=a.glyph('中',18);count=len(a.updates)
        self.assertEqual(one,a.glyph('中',18));self.assertEqual(len(a.updates),count)
    def test_transform_and_triangle_stride(self):
        c=Canvas(None);c.translate(12,24);c.quad(0,0,10,10)
        self.assertEqual(len(c.vertices),48)
        self.assertEqual(tuple(c.vertices[:2]),(12,24))
    def test_text_width_bound(self):
        a=Atlas();c=Canvas(a);c.text('abcdef',10,10,18,width=20)
        self.assertLess(len(c.vertices),6*6*8)
    def test_terminal_sgr(self):
        self.assertEqual(list(spans('\x1b[31mFAIL\x1b[0m ok')),[('FAIL','#AF2337'),(' ok','text')])
    def test_terminal_cell_width(self):
        self.assertEqual((cells('a'),cells('中'),cells('\u0301')),(1,2,0))
    def test_full_cpu_number_is_a_single_bounded_label(self):
        labels=[]
        class SpyCanvas(Canvas):
            def text(self,value,x,y,size,*args,**kwargs):
                labels.append((value,x,y,size))
                return super().text(value,x,y,size,*args,**kwargs)
        c=SpyCanvas(Atlas());draw_overview(c,516,1002,{'cpu':100})
        label=next(row for row in labels if row[0]=='负载 100.0%')
        self.assertLess(label[1]+sum(c.atlas.glyph(ch,label[3])[4] for ch in label[0]),382)



if __name__=='__main__':unittest.main()
