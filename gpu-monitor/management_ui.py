"""Detail pages and confirmed management controls using the existing GPU canvas."""
import math
import time
from metrics import quantity
from scene import color, panel, text, line, progress, history_bars, percent, temperature, gpu_percent
from management_model import ManagementModel
from management_backend import validate_thresholds, ManagementError


def number(value, suffix='', digits=1):
    return '--' if value is None else f'{value:.{digits}f}'+suffix


def pressure_value(details, resource, field='some'):
    return (details.get('pressure', {}).get(resource) or {}).get(field)


def fresh(snapshot):
    return snapshot is not None and 0 <= time.monotonic()-snapshot.get('stamp',0) <= 15


class ManagementUI:
    def management_init(self):
        self.detail = None;self.service_view = False;self.service_selected = None
        self.service_page = 0;self.log_page = 0;self.show_logs = False
        self.management = ManagementModel();self.management_pending = None
        self.power_draft = None;self.power_base = None;self.handled_job = None

    def management_poll(self):
        view = None;name = None
        if self.tab == 'settings' and self.settings_section == 'power':view = 'power'
        elif self.tab == 'processes' and self.service_view:
            view = 'logs' if self.show_logs else 'apps';name = self.service_selected
        changed = self.management.poll(view, name)
        result = self.management.result
        if result and result['phase'] != 'running' and result['job'] != self.handled_job:
            self.handled_job = result['job']
            if result.get('op') == 'charge':
                self.power_draft = None;self.power_base = None;self.management.power = None
            self.management.next_refresh = 0
        p = self.management_pending
        if p and not p.get('armed') and time.monotonic() >= p['ready_at']:
            p['armed'] = True;changed = True
        return changed

    def page_header(self, c, title, action=('m_back', None)):
        self.button(c, '‹ 返回', (12,154,104,48), action, small=True)
        text(c, title, 132,157,25,bold=True,width=380)

    def detail_page(self, c, d):
        titles = {'cpu':'CPU 详情','gpu':'GPU 详情','memory':'内存与交换','storage':'存储空间'}
        self.page_header(c,titles[self.detail])
        panel(c,12,214,516,914)
        extra = d.get('details', {});rows = [];history = [];caption = ''
        if self.detail == 'cpu':
            rows = [('整机 CPU 占用',number(d.get('cpu'),'%')),('CPU 温度',temperature(d.get('cpu_temperature')))]
            for p in extra.get('cpu_policies', [])[:2]:
                rows.append(('CPU '+p['cpus'],number(p.get('frequency_khz')/1000 if p.get('frequency_khz') else None,' MHz',0)))
            rows += [('CPU 资源等待 · 10 秒',number(pressure_value(extra,'cpu'),'%')),
                     ('I/O 全部任务等待 · 10 秒',number(pressure_value(extra,'io','full'),'%'))]
            history=d.get('history',[]);caption='硬件报告频率 · 资源等待不等于占用率'
        elif self.detail == 'gpu':
            state=extra.get('gpu_state')
            status='不可用' if state is None else '已上电' if state['powered'] else '已断电 / 休眠'
            freq=state.get('frequency_khz') if state else None
            rows=[('驱动整数占用',gpu_percent(d.get('gpu'))),('供电状态',status),
                  ('硬件频率',number(freq/1000 if freq else None,' MHz',0)),
                  ('驱动分配内存',quantity(extra.get('gpu_memory'))),
                  ('最近完成绘制',number(d.get('render_ms'),' ms',2)),
                  ('已完成绘制帧数',str(d.get('render_frames','--')))]
            history=d.get('gpu_history',[]);caption='静态页占用可为 0% · 不把缓存档位当实际频率'
        elif self.detail == 'memory':
            rows=[('物理内存已用',quantity(d.get('memory_used'))),('物理内存可用',quantity(d.get('memory_available'))),
                  ('交换已用 / 总量',quantity(d.get('swap_used'))+' / '+quantity(d.get('swap_total'))),
                  ('交换读入',quantity(extra.get('swap_in'),True)),('交换写出',quantity(extra.get('swap_out'),True)),
                  ('主缺页 / 秒',number(extra.get('major_faults'))),
                  ('内存全部任务等待 · 10 秒',number(pressure_value(extra,'memory','full'),'%'))]
            history=extra.get('memory_history',[]);caption='物理内存占用历史 · Swap 为磁盘交换，不是 RAM'
        else:
            for i,row in enumerate(extra.get('volumes',[])):
                y=240+i*286
                text(c,row['label'],32,y,24,bold=True)
                text(c,'可用 '+quantity(row['available']),32,y+49,28,'blue_text',True,width=470)
                text(c,'已用 '+quantity(row['used'])+' / 总量 '+quantity(row['total']),32,y+99,16,'muted',width=470)
                ratio=row['used']/row['total'] if row['total'] and row['used'] is not None else None
                progress(c,32,y+146,476,ratio,12)
            text(c,'系统根卷与数据卷分别统计。',32,864,18,width=476)
            text(c,'数据区还有空间，不代表系统根卷也充足。',32,905,15,'muted',width=476)
            text(c,'此页不删除文件，不修改交换空间。',32,946,15,'muted',width=476)
        for i,(name,value) in enumerate(rows):
            y=238+i*76
            text(c,name,32,y,15,'muted',width=290)
            text(c,value,32,y+27,22,bold=True,width=476)
        if self.detail != 'storage':
            text(c,'最近 60 秒 · 固定 0–100%',32,846,15,'muted')
            history_bars(c,history,32,886,476,150,d.get('stamp',time.monotonic()))
            text(c,caption,32,1062,13,'muted',width=476)
        age=time.monotonic()-d.get('stamp',0)
        if age>9:text(c,'数据已过期，等待重新采样',32,1100,13,'error')

    def management_note(self,c):
        m=self.management
        message=m.error or m.message or ('正在读取…' if m.loading else '')
        if message:text(c,message,28,1076,13,'error' if m.error else 'blue_text',width=484)

    def power_settings(self,c):
        panel(c,12,214,516,914)
        text(c,'电源与充电',28,232,25,bold=True)
        self.button(c,'刷新',(398,228,114,44),('m_refresh',None),small=True)
        p=self.management.power
        if not p:
            text(c,'正在读取充电配置…',28,306,18,'muted');self.management_note(c);return
        cfg=p['config'];runtime=p.get('runtime') if fresh(p) else None;sample=(runtime or {}).get('sample',{})
        if self.power_draft is None:
            self.power_draft=[cfg['stop_percent'],cfg['resume_percent']];self.power_base=p['sha256']
        states={'external_power_charge_held':'外部供电 · 电池暂停充电','normal_charging':'正常充电策略',
                'disabled':'阈值控制未启用','waiting_for_normal_power_path':'等待正常电源通路','restore_pending':'正在恢复电源控制'}
        state='状态不可用 / 已过期' if not runtime else states.get(runtime.get('state'),runtime.get('state','未知状态'))
        text(c,state,28,294,18,'blue_text',width=484)
        text(c,'阈值控制：'+('已启用' if cfg['enabled'] else '未启用'),28,331,15,'muted')
        text(c,'电量 '+number(sample.get('soc'),'%',0)+'   电池 '+number(sample.get('battery_c'),'°C'),28,376,20,width=484)
        text(c,'电池 '+number(sample.get('battery_uv')/1e6 if sample.get('battery_uv') is not None else None,' V',3)+
             '   '+number(sample.get('battery_ma'),' mA',0),28,419,17,width=484)
        text(c,'USB 输入 '+number(sample.get('vbus_mv')/1000 if sample.get('vbus_mv') is not None else None,' V',2)+
             '   '+number(sample.get('ibus_ma'),' mA',0),28,461,17,width=484)
        power=sample.get('vbus_mv',0)*sample.get('ibus_ma',0)/1e6 if sample.get('vbus_mv') is not None and sample.get('ibus_ma') is not None else None
        text(c,'USB 输入功率估算 '+number(power,' W',2),28,502,17,width=484)
        line(c,28,548,512,548)
        for i,title in enumerate(('达到此电量停充','低于此电量恢复')):
            x=28+i*250
            panel(c,x,572,234,184,'inset')
            text(c,title,x+12,586,15,'muted')
            text(c,str(self.power_draft[i])+'%',x+12,621,30,'blue_text',True,width=210,align='center')
            self.button(c,'−',(x+12,690,96,48),('m_threshold',(i,-1)))
            self.button(c,'＋',(x+126,690,96,48),('m_threshold',(i,1)))
        valid=True
        try:validate_thresholds(*self.power_draft)
        except ManagementError:valid=False
        if valid and fresh(p) and not self.management.busy and self.power_base==p['sha256']:
            self.button(c,'保存阈值…',(28,790,484,56),('m_charge_confirm',None),True)
        else:text(c,'阈值需相差至少 3%；配置变化时请刷新。',28,806,15,'error',width=484)
        text(c,'保存需要确认；不会主动放电。',28,883,16,'muted',width=484)
        text(c,'电池仍连接，不等于物理断开电池。',28,923,16,'muted',width=484)
        text(c,'保持现有温控及电流、电压保护。',28,963,16,'muted',width=484)
        self.management_note(c)

    def services(self,c):
        self.page_header(c,'托管应用',('m_processes',None))
        panel(c,12,214,516,914)
        self.button(c,'刷新',(398,228,114,44),('m_refresh',None),small=True)
        rows=self.management.apps['rows']
        if self.service_selected:
            self.button(c,'‹ 应用列表',(28,228,160,44),('m_apps',None),small=True)
            text(c,self.service_selected,28,294,24,bold=True,width=484)
            if self.show_logs:
                text(c,'最近日志 · 只读 · 最多 60 行',28,341,14,'muted')
                logs=self.management.logs;pages=max(1,math.ceil(len(logs)/14));self.log_page=min(self.log_page,pages-1)
                for i,item in enumerate(logs[self.log_page*14:(self.log_page+1)*14]):
                    text(c,item,28,388+i*42,13,mono=True,width=484)
                self.button(c,'上一页',(28,1000,116,48),('m_log_page',-1))
                self.button(c,'下一页',(396,1000,116,48),('m_log_page',1))
                text(c,f'{self.log_page+1} / {pages}',196,1013,16,'muted',width=148,align='center')
            else:
                row=next((r for r in rows if r['name']==self.service_selected),None)
                if not row:text(c,'应用已移除，请返回列表刷新',28,352,17,'error')
                else:
                    text(c,'运行：'+('未知' if row['running'] is None else '运行中' if row['running'] else '已停止'),28,353,19)
                    text(c,'开机启动：'+('未知' if row['enabled'] is None else '已启用' if row['enabled'] else '未启用'),28,399,19)
                    text(c,row['status'],28,449,14,'muted',width=484)
                    if row['sha256'] and fresh(self.management.apps) and not self.management.busy:
                        self.button(c,'停用并关闭自启' if row['enabled'] else '启用并开机启动',(28,516,484,58),
                                    ('m_app_confirm','disable' if row['enabled'] else 'enable'),True)
                        if row['enabled']:self.button(c,'重启应用…',(28,594,484,58),('m_app_confirm','restart'))
                        self.button(c,'查看最近日志',(28,672,484,58),('m_logs',None))
                    if not fresh(self.management.apps):text(c,'列表已过期，请刷新后操作。',28,757,15,'error',width=484)
                    text(c,'仅管理已注册应用，不控制核心系统服务。',28,810,15,'muted',width=484)
                    text(c,'停止应用可能中断其任务，操作前会再次确认。',28,851,15,'muted',width=484)
        else:
            text(c,'已有应用 · 核心系统服务不提供启停',28,294,15,'muted',width=484)
            pages=max(1,math.ceil(len(rows)/6));self.service_page=min(self.service_page,pages-1)
            for i,row in enumerate(rows[self.service_page*6:(self.service_page+1)*6]):
                y=346+i*102
                panel(c,28,y,484,88,'inset')
                text(c,row['name'],42,y+12,20,bold=True,width=340)
                state='配置不可用' if not row['sha256'] else '状态未知' if row['running'] is None else '运行中' if row['running'] else '已停止'
                text(c,state,42,y+51,14,'muted')
                self.hits.append(((28,y,484,88),('m_select_app',row['name'])))
            if not rows:text(c,'尚无托管应用，请先通过 frl-app 注册。',28,376,16,'muted',width=484)
            self.button(c,'上一页',(28,982,116,48),('m_app_page',-1))
            self.button(c,'下一页',(396,982,116,48),('m_app_page',1))
            text(c,f'{self.service_page+1} / {pages}',196,995,16,'muted',width=148,align='center')
            if self.management.apps.get('truncated'):text(c,'仅显示前 32 个应用',28,1045,13,'muted')
        self.management_note(c)

    def management_dialog(self,c):
        p=self.management_pending;self.hits=[];self.slider_box=None
        color(c,'text',.25);c.quad(0,0,540,1200);panel(c,28,340,484,502)
        text(c,'确认操作',48,370,25,bold=True)
        for i,message in enumerate(p['lines']):text(c,message,48,438+i*42,17,'muted',width=444)
        self.button(c,'取消',(44,734,218,60),('m_cancel',None))
        if time.monotonic()>=p['ready_at'] and not self.management.busy:
            self.button(c,'确认执行',(278,734,218,60),('m_execute',None),True)
        else:text(c,'请先阅读…',288,752,18,'muted',width=198,align='center')

    def management_activate(self,kind,value):
        if kind=='detail':self.detail=value;return True
        if not kind.startswith('m_'):return False
        m=self.management
        if kind=='m_back':self.detail=None
        elif kind=='m_services':self.service_view=True;self.service_selected=None;self.show_logs=False;m.next_refresh=0
        elif kind=='m_processes':self.service_view=False
        elif kind=='m_apps':self.service_selected=None;self.show_logs=False;m.next_refresh=0
        elif kind=='m_select_app':self.service_selected=value;self.show_logs=False
        elif kind=='m_app_page':self.service_page=max(0,self.service_page+value)
        elif kind=='m_log_page':self.log_page=max(0,self.log_page+value)
        elif kind=='m_logs':self.show_logs=True;self.log_page=0;m.logs=[];m.next_refresh=0
        elif kind=='m_refresh':
            m.next_refresh=0
            if self.tab=='settings':self.power_draft=None;self.power_base=None
        elif kind=='m_threshold':
            if self.power_draft:
                index,delta=value;low,high=(50,95) if index==0 else (40,92)
                self.power_draft[index]=max(low,min(high,self.power_draft[index]+delta))
        elif kind=='m_charge_confirm':
            if not fresh(m.power) or not self.power_draft or m.busy:raise ManagementError('配置尚未就绪或已过期')
            validate_thresholds(*self.power_draft)
            if self.power_base!=m.power['sha256']:raise ManagementError('配置已变化，请刷新')
            self.management_pending={'request':{'op':'charge','expected':self.power_base,'stop':self.power_draft[0],'resume':self.power_draft[1]},
                'lines':[f'达到 {self.power_draft[0]}% 停充，低于 {self.power_draft[1]}% 恢复。',
                         '保持当前启用状态及温控保护。','保存后立即按新阈值判断，不主动放电。'],
                'ready_at':time.monotonic()+.6}
        elif kind=='m_app_confirm':
            row=next((r for r in m.apps['rows'] if r['name']==self.service_selected),None)
            if not row or not row['sha256'] or not fresh(m.apps) or m.busy:raise ManagementError('应用信息尚未就绪或已过期')
            title={'enable':'启用应用，并开启开机启动','disable':'停止应用，并关闭开机启动','restart':'重启应用，中断当前任务'}[value]
            self.management_pending={'request':{'op':'app','expected':row['sha256'],'name':row['name'],'verb':value},
                'lines':[row['name'],title,'请保存相关工作；不会操作其他应用。'], 'ready_at':time.monotonic()+.6}
        elif kind=='m_cancel':self.management_pending=None
        elif kind=='m_execute':
            p=self.management_pending
            if not p or time.monotonic()<p['ready_at']:raise ManagementError('请阅读后确认')
            m.submit(p['request']);self.management_pending=None
        else:raise ManagementError('未知管理动作')
        return True
