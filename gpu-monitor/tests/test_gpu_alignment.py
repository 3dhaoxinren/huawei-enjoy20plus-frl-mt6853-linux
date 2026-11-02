"""GPU text anchors, row baseline, and long-value containment."""
import unittest
from unittest import mock
import scene
from canvas import Atlas, Canvas
from scene import draw_overview


class Spy(Canvas):
    def __init__(self):
        super().__init__(Atlas())
        self.labels = {}
    def text(self, value, x, y, size=18, bold=False, width=None, align=None, mono=False):
        start = len(self.vertices)
        super().text(value, x, y, size, bold, width, align, mono)
        self.labels[str(value)] = (x, y, size, width, align, self.vertices[start:])


class AlignmentTests(unittest.TestCase):
    def render(self, frames):
        cr = Spy()
        draw_overview(cr, 516, 1002, {'gpu':0, 'render_frames':frames})
        return cr.labels
    def test_shared_left_edge_and_row_spacing(self):
        labels = self.render(123)
        rows = [labels[s] for s in ('Mali-G57 MC3', 'GLES 3.2', '整数占用 · 静态页面读数可为 0%')]
        self.assertEqual([r[0] for r in rows], [158]*3)
        self.assertEqual([r[1] for r in rows], [205.25,237.25,269.25])
        self.assertEqual(labels['已绘制 123 帧'][1:3], labels['GLES 3.2'][1:3])
        self.assertEqual(labels['已绘制 123 帧'][0]+labels['已绘制 123 帧'][3], 498)
    def test_frames_do_not_shift_version_or_exceed_card(self):
        for frames in (0,9,10,999999,10**30):
            labels = self.render(frames)
            self.assertEqual(labels['GLES 3.2'][:5], (158,237.25,14,100,None))
            row = labels[f'已绘制 {frames} 帧']
            self.assertEqual(row[4], 'right')
            # Glyph atlas adds a 1 px transparent border; card ends at 516.
            self.assertGreaterEqual(min(row[5][0::8]),269)
            self.assertLessEqual(max(row[5][0::8]),500)
    def test_missing_frames_uses_same_alignment(self):
        row = self.render(None)['等待渲染数据']
        self.assertEqual(row[:5], (270,237.25,14,228,'right'))
    def test_text_group_center_matches_ring(self):
        with mock.patch.object(scene, 'ring', wraps=scene.ring) as ring:
            labels=self.render(123)
        gpu_ring=next(call.args for call in ring.call_args_list if call.args[-1]=='GPU')
        title_y=labels['Mali-G57 MC3'][1]
        # Measured native glyph ink offsets, not the nominal font boxes.
        self.assertEqual((title_y+4+title_y+77.5)/2, gpu_ring[2])
    def test_history_stays_below_centered_text(self):
        with mock.patch.object(scene, 'history_bars', wraps=scene.history_bars) as bars:
            labels=self.render(123)
        gpu_chart=next(call.args for call in bars.call_args_list if call.args[2]==158)
        self.assertEqual(gpu_chart[3:6], (292,340,16))
        self.assertGreater(gpu_chart[3], labels['Mali-G57 MC3'][1]+77.5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
