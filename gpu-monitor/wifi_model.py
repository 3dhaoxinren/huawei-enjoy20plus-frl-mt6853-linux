"""Nonblocking scan/model for the GPU settings UI. Secret input travels via pipe."""
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
import uuid
from wifi_core import Control,RUNTIME,WifiError,parse_scan,private_runtime,saved_ssids,status


class WifiModel:
    def __init__(self):
        self.networks=[];self.current={};self.message='点击刷新搜索附近 Wi-Fi'
        self.busy=False;self.scanning=False;self.revision=0;self.job=None;self.worker=None
        self.next_poll=0;self.started=0;self.last_result=None
        self._scan_result=None;self._guard=threading.Lock()
        self._scan_generation=0

    def _publish_scan(self,generation,rows,current,message,complete,preserve):
        # One bounded mailbox. A newer completed result may replace a cached one.
        with self._guard:
            if generation==self._scan_generation:
                self._scan_result=(generation,rows,current,message,complete,preserve)

    def _consume_scan(self):
        with self._guard:
            result,self._scan_result=self._scan_result,None
        if result is None:return False
        generation,rows,current,message,complete,preserve=result
        if generation!=self._scan_generation:return False
        if rows is not None:self.networks=rows
        if current is not None:self.current=current
        self.scanning=not complete
        if not self.busy and not preserve:self.message=message
        return True

    @staticmethod
    def _snapshot(control):
        current=status(control)
        rows=parse_scan(control.request('SCAN_RESULTS'),
                        current['ssid_hex'] if current['connected'] else '',saved_ssids(control))
        return rows,current

    def refresh(self,preserve_message=False):
        if self.scanning or self.busy:return
        self.scanning=True
        with self._guard:
            self._scan_generation+=1;generation=self._scan_generation
        if not preserve_message:self.message='正在扫描附近 Wi-Fi…'
        self.revision+=1
        def run():
            rows=current=None
            try:
                with Control() as control:
                    # Display known data immediately; never label the cache as a fresh scan.
                    try:
                        rows,current=self._snapshot(control)
                        message='显示上次扫描结果 · 正在刷新…' if rows else '正在扫描附近 Wi-Fi…'
                        self._publish_scan(generation,rows,current,message,False,preserve_message)
                    except Exception:pass  # A cache miss must not suppress a fresh scan.
                    fresh=control.scan_wait()
                    rows,current=self._snapshot(control)
                result=(rows,current,('已找到 '+str(len(rows))+' 个网络') if fresh else '扫描未完成，显示上次结果；可重试')
            except Exception:result=(rows,current,'刷新失败，已有列表保留；请稍后重试')
            self._publish_scan(generation,*result,True,preserve_message)
        threading.Thread(target=run,daemon=True,name='wifi-scan').start()

    def connect(self,network,password):
        if self.busy:raise WifiError('正在连接，请稍候')
        if network['security']=='unsupported':raise WifiError('暂不支持此加密方式，仅支持 WPA2 / 开放网络')
        private_runtime();job=uuid.uuid4().hex
        request={'job':job,'ssid_hex':network['ssid_hex'],'security':network['security'],'password':password}
        payload=json.dumps(request).encode()
        worker=subprocess.Popen(['/usr/bin/python3','-B',str(Path(__file__).parent/'wifi_worker.py')],
                                stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                start_new_session=True,close_fds=True)
        try:worker.stdin.write(payload);worker.stdin.close()
        finally:request['password']='';payload=b''
        # A scan started before connection must not overwrite the new connection state.
        with self._guard:
            self._scan_generation+=1;self._scan_result=None
        self.scanning=False
        self.worker=worker;self.job=job;self.started=time.monotonic()
        self.busy=True;self.message='正在连接，切换网络可能改变 IP…';self.revision+=1

    def poll(self):
        now=time.monotonic();changed=self._consume_scan()
        # Cached scan results are delivered on the next UI tick (~50 ms), not the
        # slower connection-job polling interval. All socket I/O stays off-thread.
        if now<self.next_poll:
            if changed:self.revision+=1
            return changed
        self.next_poll=now+.4
        if self.busy:
            try:
                path=RUNTIME/'status.json';info=path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077 or info.st_size>4096:
                    raise ValueError('untrusted Wi-Fi status')
                value=json.loads(path.read_bytes())
                if value.get('job')==self.job and value!=self.last_result:
                    self.message=str(value.get('message',''))[:100];self.last_result=value;changed=True
                    if value.get('phase') in ('connected','failed'):
                        self.busy=False
                        if value.get('phase')=='connected':
                            self.current={'connected':True,'ssid':value.get('ssid',''),'ip':value.get('ip','')}
                        self.refresh(preserve_message=True)
            except (OSError,ValueError,TypeError):pass
            if self.busy and self.worker.poll() is not None:
                self.busy=False;self.message='连接任务结束，请刷新确认网络状态';changed=True
            if self.busy and now-self.started>55:
                self.busy=False;self.message='连接超时，原网络配置保留';changed=True
        if changed:self.revision+=1
        return changed
