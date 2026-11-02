"""Real telemetry and touch UI expressed entirely as GPU triangles/textured glyphs."""
import math
import time
from canvas import Canvas
from input_keys import key_bytes, SHIFT_DIGITS
from metrics import quantity
from scene import color, panel, text, line, progress, draw_header, draw_overview, draw_compact, percent, draw_navigation
from terminal import spans, cells
from display_settings import OPTIONS, label, load_settings, save_settings
from display_settings import load_brightness, save_brightness, preview_brightness, clear_brightness_preview
from wifi_model import WifiModel
from process_actions import ProcessActions
from management_ui import ManagementUI


class Dashboard(ManagementUI):
    def __init__(self, atlas, terminal):
        self.atlas, self.terminal = atlas, terminal
        self.tab = 'overview'
        self.keyboard = True
        self.shift = self.ctrl = self.symbols = False
        self.sort, self.page = 'cpu', 0
        self.hits = []
        self.error = None
        self.actions = 0
        self.idle_off_seconds, self.settings_error = load_settings()
        self.brightness_percent, self.brightness_error = load_brightness()
        self.slider_drag=False;self.slider_box=None
        self.slider_sent=None;self.slider_stamp=-10
        self.settings_section='display';self.wifi=None;self.wifi_page=0
        self.wifi_selected=None;self.wifi_password=''
        self.process_actions=ProcessActions()
        self.management_init()

    def button(self, c, label, box, action, selected=False, small=False):
        x, y, w, h = box
        panel(c, x, y, w, h, 'selected' if selected else 'surface')
        text(c, label, x+5, y+(h-(15 if small else 18)*1.3)/2,
             15 if small else 18, 'blue_text' if selected else 'text', selected,
             width=w-10, align='center')
        self.hits.append((box, action))

    def render(self, data):
        c = Canvas(self.atlas)
        self.hits = []
        self.slider_box=None
        color(c, 'background');c.quad(0,0,540,1200)
        c.save();c.translate(12, 12);draw_header(c, 516, 112, data);c.restore()
        if self.tab == 'overview' and self.detail:
            self.detail_page(c,data)
        elif self.tab == 'overview':
            c.save();c.translate(12, 126);draw_overview(c, 516, 1002, data);c.restore()
            self.hits.append(((12,856,516,90),('open_wifi',None)))
            self.hits.append(((12,464,252,198),('tab','processes')))
            self.hits.extend([((12,126,516,154),('detail','memory')),
                              ((12,292,516,160),('detail','gpu')),
                              ((276,464,252,198),('detail','cpu')),
                              ((12,662,516,182),('detail','cpu')),
                              ((276,958,252,142),('detail','storage')),
                              ((12,958,252,142),('open_power',None))])
        elif self.tab == 'processes':
            if self.service_view:self.services(c)
            else:self.processes(c, data)
        elif self.tab == 'settings':
            self.settings(c)
        else:
            self.draw_terminal(c, data)
        self.hits.append(((480,48,48,52),('open_display',None)))
        draw_navigation(c,self.tab)
        for i, tab in enumerate(('overview','processes','terminal','settings')):
            self.hits.append(((i*135,1140,135,60),('tab',tab)))
        if self.error:
            error_y=708 if self.tab=='settings' and self.wifi_selected is not None else 1086
            panel(c,12,error_y,516,44,'selected')
            text(c,self.error,24,error_y+11,14,'error',width=492)
        if self.process_actions.pending:self.process_dialog(c)
        if self.management_pending:self.management_dialog(c)
        return c

    def poll(self):
        if self.slider_drag:self.update_preview()
        changed=self.wifi.poll() if self.wifi else False
        managed=self.management_poll()
        return self.process_actions.poll() or changed or managed

    def process_dialog(self,c):
        p=self.process_actions.pending;target=p['target'];stage=p['stage']
        self.hits=[];self.slider_box=None
        color(c,'text',.25);c.quad(0,0,540,1200)
        panel(c,28,288,484,628)
        titles={'blocked':'系统保护','warning':'重要进程 · 影响提示','confirm':'确认结束进程',
                'sent':'正在结束','done':'操作结果','failed':'未执行结束'}
        text(c,titles[stage],48,312,25,bold=True,width=444)
        text(c,target['name'],48,365,23,bold=True,width=444)
        text(c,f"PID {target['pid']}  ·  UID {target['uid']}",48,404,15,'muted')
        for i,message in enumerate(p['impact'][:4]):
            text(c,message,48,458+i*34,16,'muted',width=444)
        text(c,'此页面不会向该进程发送信号。' if stage=='blocked' else '只发送 SIGTERM，不自动强杀。',48,617,16,width=444)
        if stage=='confirm' and p['risk']=='important':
            text(c,'二次确认：仍要结束这个进程吗？',48,659,17,'error',width=444)
        message=self.error or p.get('message')
        if message:text(c,message,48,710,15,'error' if stage=='failed' or self.error else 'blue_text',width=444)
        if stage in ('warning','confirm'):
            self.button(c,'取消',(44,806,218,60),('process_cancel',None))
            armed=time.monotonic()>=p['ready_at']
            label=('我已了解，继续' if stage=='warning' else '确认结束') if armed else '请先阅读…'
            if stage=='confirm' and armed:
                panel(c,278,806,218,60,'danger_surface')
                text(c,label,288,824,18,'error',True,width=198,align='center')
                self.hits.append(((278,806,218,60),('process_end',None)))
            else:
                self.button(c,label,(278,806,218,60),('process_next',None),True)
                if not armed:self.hits.pop()
        else:self.button(c,'关闭',(44,806,452,60),('process_cancel',None))

    def update_preview(self,force=False):
        now=time.monotonic()
        if force or (now-self.slider_stamp>=.05 and self.slider_sent!=self.brightness_percent) or now-self.slider_stamp>=1:
            preview_brightness(self.brightness_percent)
            self.slider_sent=self.brightness_percent;self.slider_stamp=now

    def slider_value(self,point):
        # Track ends x=44..496, absolute coordinates; pointer is captured during drag.
        return round(1+99*max(0,min(1,(point[0]-44)/452)),2)

    def cancel_slider(self):
        if self.slider_drag:
            self.slider_drag=False
            try:clear_brightness_preview()
            finally:self.brightness_percent,self.brightness_error=load_brightness()

    def pointer(self,phase,start,end):
        if phase=='cancel':
            active=self.slider_drag;self.cancel_slider();return active
        if phase=='down' and self.slider_box:
            x,y,w,h=self.slider_box
            if x<=end[0]<x+w and y<=end[1]<y+h:
                self.slider_drag=True
        if self.slider_drag:
            self.brightness_percent=self.slider_value(end)
            if phase=='up':
                try:save_brightness(self.brightness_percent)
                finally:self.cancel_slider()
                self.actions+=1
            else:self.update_preview()
            return True
        return self.touch(start,end) if phase=='up' else False

    def settings(self,c):
        for i,(key,title) in enumerate((('display','显示'),('wifi','Wi-Fi'),('power','电源'))):
            self.button(c,title,(12+i*176,154,164,44),('section',key),self.settings_section==key)
        if self.settings_section=='display':
            # Settings body has its own GPU coordinate transform; touch boxes stay absolute.
            c.save();c.translate(0,60)
            before=len(self.hits);self.display_settings(c)
            self.hits[before:]=[((x,y+60,w,h),a) for (x,y,w,h),a in self.hits[before:]]
            c.restore()
        elif self.settings_section=='power':
            self.power_settings(c)
        else:
            self.wifi_settings(c)

    def wifi_settings(self,c):
        panel(c,12,214,516,914)
        text(c,'Wi-Fi',28,232,26,bold=True)
        self.button(c,'刷新',(390,228,122,44),('wifi_refresh',None),small=True)
        current=self.wifi.current
        message='未连接' if not current.get('connected') else current.get('ssid','')+' · '+current.get('ip','')
        text(c,message,28,281,15,'blue_text',width=484)
        text(c,self.wifi.message,28,313,13,'muted',width=484)
        if self.wifi_selected is not None:
            net=self.wifi_selected
            text(c,net['ssid'],28,366,24,bold=True,width=484)
            text(c,'开放网络：连接不加密，请确认网络可信。' if net['security']=='open' else 'WPA2 · 密码仅用于连接，不显示在日志中。',
                 28,410,14,'muted',width=484)
            if net['security']=='psk':
                panel(c,28,451,484,58,'inset')
                shown='•'*min(28,len(self.wifi_password)) if self.wifi_password else ('已保存，可留空直接连接' if net.get('saved') else '请用下方键盘输入密码')
                text(c,shown,42,468,19,'text' if self.wifi_password else 'muted',width=452)
            text(c,'切换网络会中断当前 SSH，新 IP 将显示在本屏。',28,552,14,'muted',width=484)
            self.button(c,'取消',(28,608,234,54),('wifi_cancel',None))
            self.button(c,'确认连接' if net['security']=='open' else '连接',(278,608,234,54),('wifi_connect',None),True)
            if net['security']=='psk':self.draw_keyboard(c)
            return
        rows=self.wifi.networks;pages=max(1,math.ceil(len(rows)/7));self.wifi_page=min(self.wifi_page,pages-1)
        for i,net in enumerate(rows[self.wifi_page*7:(self.wifi_page+1)*7]):
            y=362+i*90
            panel(c,28,y,484,78,'selected' if net['connected'] else 'surface')
            text(c,net['ssid'],42,y+10,19,bold=net['connected'],width=340)
            state='已连接' if net['connected'] else '已保存' if net['saved'] else 'WPA2' if net['security']=='psk' else '开放网络' if net['security']=='open' else '暂不支持'
            text(c,state,42,y+44,13,'blue_text' if net['connected'] else 'muted')
            text(c,str(net['strength'])+' dBm',382,y+28,13,'muted',width=115,align='right')
            self.hits.append(((28,y,484,78),('wifi_select',net)))
        if not rows and not self.wifi.scanning:
            text(c,'未发现可用网络，请点击刷新。',28,384,17,'muted')
        self.button(c,'上一页',(28,1032,116,48),('wifi_page',-1))
        self.button(c,'下一页',(396,1032,116,48),('wifi_page',1))
        text(c,f'{self.wifi_page+1} / {pages}',184,1047,17,'muted',width=170,align='right')
        text(c,'支持 WPA2 与开放网络；连接成功后自动保存。',28,1096,13,'muted',width=484)

    def wifi_key(self,value):
        if value=='Esc':self.activate(('wifi_cancel',None))
        elif value=='⌫':self.wifi_password=self.wifi_password[:-1]
        elif value=='Ctrl-C':self.wifi_password=''
        elif value=='Enter':self.activate(('wifi_connect',None))
        else:
            raw=key_bytes(value,self.shift,False)
            if raw:
                character=raw.decode('utf-8')
                if character.isprintable() and len((self.wifi_password+character).encode('utf-8'))<=64:
                    self.wifi_password+=character
        self.shift=self.ctrl=False

    def display_settings(self, c):
        self.idle_off_seconds, self.settings_error = load_settings()
        panel(c,12,154,516,552)
        text(c,'显示设置',28,175,26,bold=True)
        text(c,'无操作后自动熄屏',28,224,18,'muted')
        for i,(seconds,title) in enumerate(OPTIONS):
            self.button(c,title,(28+(i%2)*250,272+(i//2)*76,234,60),
                        ('timeout',seconds),self.idle_off_seconds==seconds)
        line(c,28,510,512,510)
        text(c,'当前：'+label(self.idle_off_seconds),28,532,20,'blue_text',bold=True)
        text(c,'按电源键直接恢复显示',28,582,18)
        text(c,'熄屏不退出终端，也不停止后台服务。',28,622,16,'muted',width=484)
        text(c,'设置自动保存，重启后继续生效。',28,657,16,'muted',width=484)
        if not self.slider_drag:self.brightness_percent,self.brightness_error=load_brightness()
        panel(c,12,724,516,300)
        text(c,'屏幕亮度',28,744,26,bold=True)
        text(c,'拖动滑条，实时调节',28,786,15,'muted')
        text(c,f'{self.brightness_percent:.0f}%',28,820,34,'blue_text',True)
        fraction=(self.brightness_percent-1)/99
        progress(c,44,898,452,fraction,8)
        color(c,'blue');c.arc(44+452*fraction,902,14,0,math.tau);c.fill()
        color(c,'surface');c.arc(44+452*fraction,902,6,0,math.tau);c.fill()
        text(c,'暗 · 1%',28,934,14,'muted')
        text(c,'100% · 亮',383,934,14,'muted',width=129,align='right')
        self.slider_box=(28,930,484,64)  # local slider y=870, plus settings transform +60.
        text(c,'松手自动保存，唤醒后保持。',28,985,15,'muted',width=484)
        if self.settings_error or self.brightness_error:
            text(c,self.settings_error or self.brightness_error,28,1036,14,'error',width=484)

    def processes(self, c, data):
        panel(c,12,154,516,976)
        text(c,'进程监控',28,172,24,bold=True)
        text(c,'点选可结束 · 重要进程二次确认 · CPU 单核口径',28,211,14,'muted',width=484)
        self.button(c,'CPU ↓',(28,247,150,44),('sort','cpu'),self.sort=='cpu')
        self.button(c,'内存 ↓',(188,247,150,44),('sort','rss'),self.sort=='rss')
        self.button(c,'服务 ›',(350,247,162,44),('m_services',None),small=True)
        rows=sorted(data.get('processes',[]),key=lambda r:r.get(self.sort) or 0,reverse=True)
        count=16;pages=max(1,math.ceil(len(rows)/count));self.page=min(self.page,pages-1)
        text(c,'PID / 名称',28,312,14,'muted')
        text(c,'CPU',349,312,14,'muted',width=62,align='right')
        text(c,'内存',430,312,14,'muted',width=80,align='right')
        line(c,28,339,512,339)
        for i,row in enumerate(rows[self.page*count:(self.page+1)*count]):
            y=350+i*44
            text(c,str(row['pid']),28,y+1,12,'muted',mono=True,width=57)
            text(c,row['name'],92,y,15,mono=True,width=225)
            text(c,percent(row['cpu'])+'%',329,y,14,mono=True,width=82,align='right')
            text(c,quantity(row['rss']),419,y,13,mono=True,width=91,align='right')
            line(c,28,y+29,512,y+29)
            self.hits.append(((28,y-3,484,44),('process',{'pid':row['pid'],'start':row.get('start')})))
        self.button(c,'上一页',(28,1058,116,48),('page',-1))
        self.button(c,'下一页',(396,1058,116,48),('page',1))
        text(c,f'{self.page+1} / {pages}',184,1072,17,'muted',width=170,align='right')

    def draw_terminal(self,c,data):
        c.save();c.translate(12,154);draw_compact(c,516,183,data);c.restore()
        bottom=796 if self.keyboard else 1128
        panel(c,12,351,516,bottom-351)
        text(c,'终端',28,367,20,bold=True)
        text(c,'frl · 会话保留',91,372,13,'muted')
        self.button(c,'收起键盘' if self.keyboard else '展开键盘',(378,359,138,40),('keyboard',None),small=True)
        line(c,28,407,512,407)
        self.terminal.resize(max(2,int((bottom-428)/20)))
        if not self.terminal.lines:
            self.terminal.capture()
        for i,row in enumerate(self.terminal.lines):
            col=0
            for part,ink in spans(row):
                for ch in part:
                    width=cells(ch)
                    if col+width>56:break
                    if ch.isprintable() and ch!=' ':
                        text(c,ch,28+col*8.6,421+i*20,14,ink,mono=True)
                    col+=width
        if self.terminal.cursor:
            x,y=self.terminal.cursor
            if 0<=x<56 and 0<=y<self.terminal.rows:
                line(c,28+x*8.6,439+y*20,36+x*8.6,439+y*20,'blue',2)
        if self.terminal.scroll:
            text(c,f'回看 {self.terminal.scroll} 行 · 输入返回底部',28,bottom-21,12,'blue_text')
        if self.keyboard:
            self.draw_keyboard(c)

    def draw_keyboard(self,c):
        rows=[[(key,1) for key in ('Esc','Tab','Ctrl','←','↓','↑','→')],
              [(key,1) for key in ('!@#$%^&*()' if self.symbols else '1234567890')],
              [(key,1) for key in ('[]{}<>\\|=+' if self.symbols else 'qwertyuiop')],
              [(key,1) for key in ('~`_-,.;:"?' if self.symbols else "asdfghjkl'")],
              [('Shift',1.5)]+[(key,1) for key in 'zxcvbnm']+[('⌫',1.5)],
              [('Ctrl-C',1.6),('#+=',1.2),('/',1),('space',4),('Enter',2.2)]]
        for i,keys in enumerate(rows):
            total=sum(span for _,span in keys);unit=(516-5*(len(keys)-1))/total;x=12
            for key,span in keys:
                label='空格' if key=='space' else ('连接' if self.tab=='settings' and self.wifi_selected is not None else '换行') if key=='Enter' else 'ABC' if key=='#+=' and self.symbols else key
                if len(key)==1 and self.shift:label=SHIFT_DIGITS.get(key,key.upper())
                active=key=='Enter' or key=='Shift' and self.shift or key=='Ctrl' and self.ctrl
                w=unit*span
                self.button(c,label,(x,810+i*53,w,47),('key',key),active,small=len(label)>2)
                x+=w+5

    def touch(self,start,end):
        if math.dist(start,end)>24:
            if self.tab=='terminal' and 410<start[1]<(796 if self.keyboard else 1128):
                self.terminal.scroll=max(0,min(getattr(self.terminal,'history',0),self.terminal.scroll+int((end[1]-start[1])/20)))
                self.terminal.capture()
                return True
            return False
        for (x,y,w,h),action in reversed(self.hits):
            if all(x<=p[0]<x+w and y<=p[1]<y+h for p in (start,end)):
                self.activate(action);self.actions+=1;return True
        return False

    def activate(self,action):
        kind,value=action
        if self.management_activate(kind,value):
            self.error=None;return
        if kind in ('tab','section','open_wifi','open_display','open_power'):
            self.cancel_slider();self.detail=None
        if kind=='process':
            self.cancel_slider();self.process_actions.select(value)
        elif kind=='process_next':self.process_actions.advance()
        elif kind=='process_end':self.process_actions.terminate()
        elif kind=='process_cancel':self.process_actions.cancel()
        elif kind=='tab':
            self.tab=value
            if value!='processes':self.service_view=False
            if value!='settings':self.wifi_password='';self.wifi_selected=None
        elif kind=='section':
            self.settings_section=value;self.wifi_password='';self.wifi_selected=None
            if value=='power':self.management.next_refresh=0
            if value=='wifi' and self.wifi is None:
                self.wifi=WifiModel();self.wifi.refresh()
        elif kind=='open_display':
            self.tab='settings';self.activate(('section','display'))
        elif kind=='open_wifi':
            self.tab='settings'
            self.activate(('section','wifi'))
            if not self.wifi.scanning and not self.wifi.busy:self.wifi.refresh()
        elif kind=='open_power':
            self.tab='settings';self.activate(('section','power'))
        elif kind=='wifi_refresh':self.wifi.refresh()
        elif kind=='wifi_page':self.wifi_page=max(0,self.wifi_page+value)
        elif kind=='wifi_select':
            if self.wifi.busy:raise RuntimeError('正在连接，请稍候')
            if value['connected']:return
            if value['security']=='unsupported':raise RuntimeError('暂不支持此加密方式，仅支持 WPA2 / 开放网络')
            self.wifi_selected=value;self.wifi_password='';self.shift=self.ctrl=self.symbols=False
        elif kind=='wifi_cancel':self.wifi_selected=None;self.wifi_password=''
        elif kind=='wifi_connect':
            if self.wifi_selected is None:return
            self.wifi.connect(self.wifi_selected,self.wifi_password)
            self.wifi_password='';self.wifi_selected=None
            self.shift=self.ctrl=self.symbols=False
        elif kind=='timeout':
            save_settings(value)
            self.idle_off_seconds=value
            self.settings_error=None
        elif kind=='brightness':
            save_brightness(value)
            self.brightness_percent=value
            self.brightness_error=None
        elif kind=='sort':self.sort=value;self.page=0
        elif kind=='page':self.page=max(0,self.page+value)
        elif kind=='keyboard':self.keyboard=not self.keyboard
        elif kind=='key':
            if value=='Shift':self.shift=not self.shift
            elif value=='Ctrl':self.ctrl=not self.ctrl
            elif value=='#+=':self.symbols=not self.symbols
            elif self.tab=='settings' and self.wifi_selected is not None:
                self.wifi_key(value)
            else:
                self.terminal.send(key_bytes(value,self.shift,self.ctrl))
                self.shift=self.ctrl=False
                self.terminal.capture()
        self.error=None
