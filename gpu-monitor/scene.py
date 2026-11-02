"""GPU scene layout for the approved cool-gray/blue instrument theme."""
import math
import time

from metrics import quantity

COLORS = {"background": "#EEF0F3", "surface": "#FFFFFF", "inset": "#F8FAFC",
          "border": "#E2E7EE", "text": "#192330", "muted": "#536171",
          "blue": "#278AE9", "blue_text": "#174EA6", "selected": "#EAF2FF",
          "chart": "#68AFF3", "track": "#ECEFF2", "green": "#218647", "error": "#AF2337",
          "danger_surface": "#FDEEF0"}


def color(cr, name, alpha=1):
    value = COLORS.get(name, name).lstrip("#")
    cr.set_source_rgba(*(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)), alpha)


def rounded(cr, x, y, w, h, radius=16):
    radius = min(radius, w / 2, h / 2)
    cr.new_sub_path()
    for cx, cy, start in ((x + w - radius, y + radius, -math.pi / 2),
                          (x + w - radius, y + h - radius, 0),
                          (x + radius, y + h - radius, math.pi / 2),
                          (x + radius, y + radius, math.pi)):
        cr.arc(cx, cy, radius, start, start + math.pi / 2)
    cr.close_path()


def panel(cr, x, y, w, h, fill="surface", border=False):
    rounded(cr, x, y, w, h)
    color(cr, fill)
    cr.fill_preserve()
    if border:
        color(cr, "border")
        cr.set_line_width(1)
        cr.stroke()
    else:
        cr.new_path()


def text(cr, value, x, y, size=18, ink="text", bold=False, width=None, align=None, mono=False):
    color(cr, ink)
    cr.text(value, x, y, size, bold, width, align, mono)


def line(cr, x1, y1, x2, y2, ink="border", thickness=1):
    color(cr, ink)
    cr.set_line_width(thickness)
    cr.move_to(x1, y1)
    cr.line_to(x2, y2)
    cr.stroke()


def progress(cr, x, y, w, fraction, h=10):
    rounded(cr, x, y, w, h, h / 2)
    color(cr, "track")
    cr.fill()
    if fraction is not None and fraction > 0:
        rounded(cr, x, y, max(2, min(1, fraction) * w), h, h / 2)
        color(cr, "blue")
        cr.fill()


def percent(value):
    return "--" if value is None else f"{value:.1f}"


def temperature(value):
    return "--" if value is None else f"{value:.1f}°C"


def gpu_percent(value):
    return "--" if value is None else f"{value:.0f}%"


def sparkline(cr, history, x, y, w, h, axes=False):
    if axes:
        for fraction in (0, .5, 1):
            line(cr, x, y + fraction * h, x + w, y + fraction * h)
            text(cr, f"{int((1-fraction)*100)}%", x + w + 5, y + fraction * h - 8, 11, "muted")
    if not history:
        return
    newest = history[-1][0]
    points = [(x + (stamp - newest + 60) / 60 * w, y + (1 - max(0, min(100, value)) / 100) * h)
              for stamp, value in history if value is not None and newest - stamp <= 60]
    if len(points) < 2:
        text(cr, "正在采样…", x, y + h / 2 - 9, 14, "muted")
        return
    color(cr, "blue", .06)
    for a, b in zip(points, points[1:]):
        cr.triangle((a[0], y+h), a, b)
        cr.triangle((a[0], y+h), b, (b[0], y+h))
    color(cr, "blue")
    cr.set_line_width(2)
    cr.move_to(*points[0])
    for px, py in points[1:]:
        cr.line_to(px, py)
    cr.stroke()


def thermometer(cr, x, y):
    color(cr, "blue_text")
    cr.set_line_width(2)
    cr.arc(x + 7, y + 28, 5, -.8, math.pi + .8)
    cr.line_to(x + 4, y + 4)
    cr.arc(x + 7, y + 4, 3, math.pi, 0)
    cr.line_to(x + 10, y + 24)
    cr.stroke()
    line(cr, x + 7, y + 10, x + 7, y + 29, "blue_text", 2)


def memory_cell(cr, x, y, w, h, label, used, total):
    panel(cr, x, y, w, h)
    text(cr, label, x + 15, y + 12, 18, bold=True)
    text(cr, quantity(used), x + 15, y + 39, 25, bold=True)
    text(cr, "/ " + quantity(total), x + 15, y + h - 30, 12 if h >= 100 else 11, "muted")
    ratio = used / total if used is not None and total and total > 0 else None
    if ratio is not None:
        text(cr, f"{ratio * 100:.1f}%", x + w - 79, y + 15, 13, "muted", width=64, align="right")
    progress(cr, x + 15, y + h - 9, w - 30, ratio, 5)


def disc(cr, x, y, radius, ink):
    cr.new_path()
    color(cr, ink)
    cr.arc(x, y, radius, 0, math.tau)
    cr.fill()


def arc_band(cr, x, y, radius, thickness, start, end, ink):
    """Joined triangle strip avoids seams between the canvas's line segments."""
    color(cr, ink)
    count = max(3, math.ceil(abs(end-start)*radius))
    for i in range(count):
        angles = (start+(end-start)*i/count, start+(end-start)*(i+1)/count)
        points = [(x+r*math.cos(a), y+r*math.sin(a))
                  for a in angles for r in (radius-thickness/2, radius+thickness/2)]
        cr.triangle(points[0], points[1], points[3])
        cr.triangle(points[0], points[3], points[2])


def ring(cr, x, y, value, label):
    radius, thickness = 51, 13
    arc_band(cr, x, y, radius, thickness, 0, math.tau, 'track')
    if value is not None and value > 0:
        end = -math.pi/2 + min(100, value)/100*math.tau
        arc_band(cr, x, y, radius, thickness, -math.pi/2, end, 'chart')
        for angle in (-math.pi/2, end):
            disc(cr, x+radius*math.cos(angle), y+radius*math.sin(angle), thickness/2, 'chart')
    text(cr, gpu_percent(value), x-47, y-22, 27, bold=True, width=94, align='center')
    text(cr, label, x-47, y+16, 16, 'muted', width=94, align='center')


def history_bars(cr, history, x, y, w, h, now):
    """Fixed 60-second / 0..100% chart; no fake minimum-height load bars."""
    line(cr, x, y+h, x+w, y+h, 'track')
    bar_width = w/21 - 1.5
    for stamp, value in history:
        if value is None or not math.isfinite(value) or value <= 0:
            continue
        age = now-stamp
        if not 0 <= age <= 60:
            continue
        px = x+(1-age/60)*(w-bar_width)
        height = min(100, value)/100*h
        color(cr, 'chart')
        cr.quad(px, y+h-height, bar_width, height)


def icon(cr, name, x, y, size=24, ink='muted'):
    """One consistent vector icon set, submitted as GPU geometry."""
    cr.save();cr.translate(x, y);cr.scale(size/24, size/24)
    color(cr, ink);cr.set_line_width(1.8)
    if name == 'settings':
        # An eight-tooth gear; circle aperture remains a stroke, not a fake glyph.
        for i in range(32):
            angle = i*math.tau/32
            radius = 10 if i % 4 in (1, 2) else 8
            point = (12+radius*math.cos(angle), 12+radius*math.sin(angle))
            if i == 0:cr.move_to(*point)
            else:cr.line_to(*point)
        cr.close_path();cr.stroke()
        arc_band(cr, 12, 12, 3.5, 1.8, 0, math.tau, ink)
    elif name == 'overview':
        cr.move_to(2, 11);cr.line_to(12, 2);cr.line_to(22, 11);cr.stroke()
        cr.move_to(5, 10);cr.line_to(5, 21);cr.line_to(19, 21);cr.line_to(19, 10);cr.stroke()
        cr.move_to(10, 21);cr.line_to(10, 15);cr.line_to(14, 15);cr.line_to(14, 21);cr.stroke()
    elif name in ('processes', 'terminal', 'system'):
        rounded(cr, 2, 3, 20, 18, 3);cr.stroke()
        if name == 'terminal':
            cr.move_to(6, 8);cr.line_to(10, 12);cr.line_to(6, 16);cr.stroke()
            cr.move_to(13, 16);cr.line_to(18, 16);cr.stroke()
        else:
            for yy in (8, 12, 16):
                cr.move_to(6, yy);cr.line_to(7, yy);cr.stroke()
                cr.move_to(10, yy);cr.line_to(18, yy);cr.stroke()
    elif name == 'wifi':
        for radius in (7, 12, 17):
            arc_band(cr, 12, 21, radius, 1.8, -math.pi*.76, -math.pi*.24, ink)
        disc(cr, 12, 21, 1.8, ink)
    elif name == 'battery':
        rounded(cr, 5, 4, 14, 19, 2);cr.stroke()
        cr.move_to(9, 1);cr.line_to(15, 1);cr.stroke()
    cr.restore()


def draw_header(cr, width, height, data):
    s = width/516
    cr.scale(s, s)
    text(cr, time.strftime('%H:%M'), 8, 0, 17, bold=True, mono=True)
    level = data.get('battery_level')
    text(cr, gpu_percent(level), 416, 0, 16, width=68, align='right')
    icon(cr, 'battery', 491, 0, 20, 'text')
    icon(cr, 'wifi', 386, 0, 18, 'text' if data.get('network_online') else 'muted')
    text(cr, 'FRL 监控', 8, 39, 30, bold=True)
    text(cr, '华为畅享 · Ubuntu ARM64', 8, 79, 16, 'muted')
    online = data.get('network_online', False)
    disc(cr, 396, 63, 4, 'green' if online else 'muted')
    text(cr, '在线' if online else '离线', 409, 51, 16, 'muted')
    icon(cr, 'settings', 478, 48, 29)


def draw_navigation(cr, active):
    color(cr, 'surface');cr.quad(0, 1140, 540, 60)
    line(cr, 0, 1140, 540, 1140)
    for i, (name, label) in enumerate((('overview', '总览'), ('processes', '进程'),
                                        ('terminal', '终端'), ('settings', '设置'))):
        ink = 'blue_text' if name == active else 'muted'
        icon(cr, name, i*135+55.5, 1146, 24, ink)
        text(cr, label, i*135, 1174, 15, ink, name == active, width=135, align='center')


def memory_ratio(used, total):
    return 100*used/total if used is not None and total and total > 0 else None


def memory_summary(cr, y, title, used, total):
    ratio = memory_ratio(used, total)
    text(cr, title, 158, y, 17, bold=True)
    text(cr, quantity(used)+' / '+quantity(total), 264, y+2, 13, 'muted', width=234, align='right')
    progress(cr, 158, y+35, 290, None if ratio is None else ratio/100, 9)
    text(cr, gpu_percent(ratio), 450, y+28, 13, 'muted', width=48, align='right')


def overview_cpu(cr, d, y, now):
    panel(cr, 0, y, 516, 380)
    text(cr, '活跃进程 ›', 18, y+16, 18, bold=True)
    text(cr, '单核口径', 154, y+20, 12, 'muted', width=86, align='right')
    line(cr, 252, y+18, 252, y+182)
    rows = d.get('processes', [])[:5]
    if not rows:text(cr, '正在采样…', 18, y+64, 15, 'muted')
    for i, row in enumerate(rows):
        yy = y+51+i*27
        icon(cr, 'terminal', 18, yy+1, 17)
        text(cr, row['name'], 44, yy, 14, width=144)
        text(cr, percent(row.get('cpu'))+'%', 188, yy, 13, width=52, align='right')
    text(cr, 'CPU', 272, y+16, 25, bold=True)
    text(cr, 'MT6853 · '+str(len(d.get('cores', [])) or '--')+' 核', 272, y+51, 16, 'muted')
    text(cr, '负载 '+percent(d.get('cpu'))+'%', 272, y+82, 17)
    text(cr, temperature(d.get('cpu_temperature')), 272, y+112, 16, 'muted')
    history_bars(cr, d.get('history', []), 382, y+88, 116, 92, now)
    text(cr, '60 秒', 272, y+157, 12, 'muted')
    line(cr, 18, y+198, 498, y+198)
    cores = {core['name']: core for core in d.get('cores', [])}
    for i in range(8):
        x, yy = 18+(i%4)*124, y+215+(i//4)*80
        core = cores.get('CPU'+str(i), {})
        text(cr, 'CPU'+str(i), x, yy, 14, bold=True)
        text(cr, gpu_percent(core.get('percent')), x+56, yy, 13, width=48, align='right')
        history_bars(cr, core.get('history', []), x, yy+28, 104, 38, now)


def draw_overview(cr, width, height, d):
    cr.scale(width/516, width/516)
    now = d.get('stamp', time.monotonic())
    panel(cr, 0, 0, 516, 154)
    ring(cr, 78, 77, memory_ratio(d.get('memory_used'), d.get('memory_total')), '内存')
    memory_summary(cr, 24, '物理内存', d.get('memory_used'), d.get('memory_total'))
    memory_summary(cr, 91, 'Swap', d.get('swap_used'), d.get('swap_total'))
    panel(cr, 0, 166, 516, 160)
    ring(cr, 78, 246, d.get('gpu'), 'GPU')
    # Center the whole visible text group on the ring (y=246), not its
    # first line. Native glyph ink spans 4..77.5 px from gpu_top.
    gpu_x, gpu_width, gpu_top = 158, 340, 246-(4+77.5)/2
    text(cr, 'Mali-G57 MC3', gpu_x, gpu_top, 23, bold=True, width=gpu_width)
    frames=d.get('render_frames')
    activity='等待渲染数据' if frames is None else f'已绘制 {frames} 帧'
    text(cr, 'GLES 3.2', gpu_x, gpu_top+32, 14, 'muted', width=100)
    text(cr, activity, gpu_x+112, gpu_top+32, 14, 'muted', width=gpu_width-112, align='right')
    detail='整数占用 · 静态页面读数可为 0%' if d.get('gpu')==0 else '驱动整数占用 · 最近 60 秒'
    text(cr, detail, gpu_x, gpu_top+64, 13, 'muted', width=gpu_width)
    # Keep the real history below the centered text, without overlap.
    history_bars(cr, d.get('gpu_history', []), gpu_x, 292, gpu_width, 16, now)
    overview_cpu(cr, d, 338, now)
    panel(cr, 0, 730, 516, 90)
    icon(cr, 'wifi', 25, 753, 46, 'blue_text')
    text(cr, 'Wi-Fi', 92, 742, 22, bold=True)
    text(cr, d.get('ipv4') or '未连接', 92, 773, 16, 'muted')
    text(cr, '↓ '+quantity(d.get('rx_rate'), True)+'   ↑ '+quantity(d.get('tx_rate'), True),
         92, 798, 13, 'muted', width=400)
    text(cr, '设置 ›', 419, 763, 17, 'blue_text', width=80, align='right')
    panel(cr, 0, 832, 252, 142)
    panel(cr, 264, 832, 252, 142)
    icon(cr, 'battery', 22, 863, 46)
    level = d.get('battery_level')
    if level is not None:
        color(cr, 'chart');cr.quad(35, 905-29*level/100, 20, 29*level/100)
    text(cr, '电池', 92, 849, 18, bold=True)
    text(cr, gpu_percent(level), 92, 881, 30, bold=True)
    text(cr, temperature(d.get('battery_temperature')), 92, 930, 18, 'muted')
    icon(cr, 'system', 282, 851, 27)
    text(cr, '系统', 324, 849, 18, bold=True)
    text(cr, 'Ubuntu ARM64', 324, 885, 16, 'muted', width=176)
    uptime = d.get('uptime')
    label = '运行 --' if uptime is None else f'已运行 {int(uptime//3600)}h {int(uptime//60)%60:02d}m'
    text(cr, label, 282, 916, 16, 'muted', width=218)
    text(cr, '存储可用 '+quantity(d.get('storage_available')), 282, 945, 14, 'muted', width=218)
    age = time.monotonic()-now
    caption = '数据已过期' if age > 9 else '部分数据不可用' if d.get('errors') else 'GPU 硬件渲染'
    text(cr, '每 3 秒更新 · 温度每 10 秒', 4, 988, 12, 'muted')
    text(cr, caption, 320, 988, 12, 'error' if age > 9 else 'muted', width=192, align='right')


def draw_compact(cr, width, height, d):
    s = width / 516
    cr.scale(s, s)
    panel(cr, 0, 0, 252, 94)
    text(cr, "CPU", 15, 12, 17, bold=True)
    text(cr, percent(d.get("cpu")) + "%", 15, 43, 29, bold=True)
    line(cr, 138, 17, 138, 77)
    text(cr, "GPU", 153, 12, 17, "muted", bold=True)
    text(cr, gpu_percent(d.get("gpu")), 153, 43, 29, "blue_text", bold=True)
    memory_cell(cr, 264, 0, 252, 94, "内存", d.get("memory_used"), d.get("memory_total"))
    for i, (label, value) in enumerate((("CPU 温度", temperature(d.get("cpu_temperature"))),
                                      ("电池温度", temperature(d.get("battery_temperature"))),
                                      ("Swap", quantity(d.get("swap_used"))))):
        x = i * 176
        panel(cr, x, 106, 164, 77)
        text(cr, label, x + 13, 116, 14, "muted")
        text(cr, value, x + 13, 141, 24 if i < 2 else 20, bold=True, width=141)
        if i == 2:
            text(cr, "/ " + quantity(d.get("swap_total")), x + 13, 166, 11, "muted")
