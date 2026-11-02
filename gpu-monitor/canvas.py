"""Triangle scene builder. Cairo is restricted to cached glyph masks, never frames."""
from array import array
import math
import cairo
import gi
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Pango, PangoCairo


class Atlas:
    def __init__(self):
        self.cache = {}
        self.updates = []
        self.x, self.y, self.row = 4, 2, 0
        self.context = cairo.Context(cairo.ImageSurface(cairo.FORMAT_A8, 1, 1))

    def glyph(self, ch, size, bold=False, mono=False):
        key = (ch, size, bold, mono)
        if key in self.cache:
            return self.cache[key]
        layout = PangoCairo.create_layout(self.context)
        font = Pango.FontDescription('DejaVu Sans Mono' if mono else 'WenQuanYi Micro Hei')
        font.set_absolute_size(size * 2 * Pango.SCALE)
        font.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        layout.set_font_description(font)
        layout.set_text(ch, -1)
        ink, logical = layout.get_pixel_extents()
        advance = logical.width / 2
        left, top = min(0, ink.x) - 2, min(0, ink.y) - 2
        w, h = max(logical.width, ink.x + ink.width) - left + 2, max(logical.height, ink.y + ink.height) - top + 2
        if self.x + w + 2 >= 2048:
            self.x, self.y, self.row = 2, self.y + self.row + 2, 0
        if self.y + h + 2 >= 2048 or w > 512 or h > 512:
            raise RuntimeError('glyph atlas capacity exceeded; no software fallback')
        surface = cairo.ImageSurface(cairo.FORMAT_A8, w, h)
        cr = cairo.Context(surface)
        cr.set_source_rgba(1, 1, 1, 1)
        cr.move_to(-left, -top)
        PangoCairo.show_layout(cr, layout)
        surface.flush()
        raw, stride = bytes(surface.get_data()), surface.get_stride()
        data = b''.join(raw[y * stride:y * stride + w] for y in range(h))
        item = (self.x, self.y, w, h, advance, left / 2, top / 2)
        self.updates.append((self.x, self.y, w, h, data))
        self.x += w + 2
        self.row = max(self.row, h)
        self.cache[key] = item
        return item


class Canvas:
    def __init__(self, atlas):
        self.atlas = atlas
        self.vertices = array('f')
        self.matrix = (1, 1, 0, 0)
        self.stack = []
        self.ink = (0, 0, 0, 1)
        self.path = []
        self.thickness = 1

    def save(self):
        self.stack.append((self.matrix, self.ink))

    def restore(self):
        self.matrix, self.ink = self.stack.pop()

    def scale(self, x, y):
        sx, sy, tx, ty = self.matrix
        self.matrix = (sx * x, sy * y, tx, ty)

    def translate(self, x, y):
        sx, sy, tx, ty = self.matrix
        self.matrix = (sx, sy, tx + sx * x, ty + sy * y)

    def set_source_rgba(self, r, g, b, a=1):
        self.ink = (r, g, b, a)

    def triangle(self, a, b, c, uv=None):
        sx, sy, tx, ty = self.matrix
        uv = uv or ((.5 / 2048, .5 / 2048),) * 3
        for (x, y), (u, v) in zip((a, b, c), uv):
            self.vertices.extend((x * sx + tx, y * sy + ty, u, v, *self.ink))

    def quad(self, x, y, w, h, uv=None):
        a, b, c, d = (x, y), (x + w, y), (x + w, y + h), (x, y + h)
        if uv:
            u, v, u2, v2 = uv
            self.triangle(a, b, c, ((u, v), (u2, v), (u2, v2)))
            self.triangle(a, c, d, ((u, v), (u2, v2), (u, v2)))
        else:
            self.triangle(a, b, c)
            self.triangle(a, c, d)

    def new_path(self):
        self.path = []

    new_sub_path = new_path

    def move_to(self, x, y):
        self.path = [(x, y)]

    def line_to(self, x, y):
        self.path.append((x, y))

    def close_path(self):
        if self.path:
            self.path.append(self.path[0])

    def arc(self, x, y, r, start, end):
        n = max(3, int(abs(end - start) * max(r, 4) / 3))
        self.path.extend((x + r * math.cos(start + (end-start)*i/n), y + r * math.sin(start + (end-start)*i/n)) for i in range(n+1))

    def rectangle(self, x, y, w, h):
        self.path = [(x, y), (x+w, y), (x+w, y+h), (x, y+h), (x, y)]

    def fill_preserve(self):
        if len(self.path) < 3:
            return
        center = (sum(x for x, _ in self.path) / len(self.path), sum(y for _, y in self.path) / len(self.path))
        for a, b in zip(self.path, self.path[1:] + self.path[:1]):
            self.triangle(center, a, b)

    def fill(self):
        self.fill_preserve()
        self.new_path()

    def set_line_width(self, width):
        self.thickness = width

    def stroke(self):
        for (x1, y1), (x2, y2) in zip(self.path, self.path[1:]):
            distance = math.hypot(x2-x1, y2-y1)
            if distance == 0:
                continue
            dx, dy = -(y2-y1)/distance*self.thickness/2, (x2-x1)/distance*self.thickness/2
            a, b, c, d = (x1+dx,y1+dy), (x2+dx,y2+dy), (x2-dx,y2-dy), (x1-dx,y1-dy)
            self.triangle(a,b,c);self.triangle(a,c,d)
        self.new_path()

    def text(self, value, x, y, size=18, bold=False, width=None, align=None, mono=False):
        chars = list(str(value))
        glyphs = [self.atlas.glyph(ch, size, bold, mono) for ch in chars]
        total = sum(g[4] for g in glyphs)
        if width is not None and total > width:
            ellipsis = self.atlas.glyph('…', size, bold, mono)
            while glyphs and total + ellipsis[4] > width:
                total -= glyphs.pop()[4]
            glyphs.append(ellipsis)
            total += ellipsis[4]
        if align == 'right' and width is not None:
            x += width - total
        elif align == 'center' and width is not None:
            x += (width - total) / 2
        for gx, gy, w, h, advance, left, top in glyphs:
            self.quad(x+left,y+top,w/2,h/2,(gx/2048,gy/2048,(gx+w)/2048,(gy+h)/2048))
            x += advance
