#!/usr/bin/python3
"""Explicit user-selected Wi-Fi transaction, isolated from display lifetime."""
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from wifi_core import (CONFIG,RUNTIME,Control,WifiError,atomic_file,credential_bytes,
                       display_name,parse_scan,private_runtime,profile,status)


def request_data(data):
    if not isinstance(data,dict) or set(data)!={'job','ssid_hex','security','password'}:
        raise WifiError('无效的连接请求')
    if not isinstance(data['job'],str) or not re.fullmatch('[a-f0-9]{32}',data['job']):raise WifiError('无效的连接请求')
    if not isinstance(data['ssid_hex'],str) or not re.fullmatch('(?:[a-fA-F0-9]{2}){1,32}',data['ssid_hex']):raise WifiError('无效的网络名称')
    if data['security'] not in ('psk','open'):raise WifiError('暂不支持此 Wi-Fi 加密方式')
    if not isinstance(data['password'],str) or len(data['password'].encode('utf-8'))>64:raise WifiError('密码过长')
    return data


class Operations:
    """Injectable boundary for tests; production only uses fixed native paths/argv."""
    def __init__(self,job,ssid):self.job=job;self.ssid=ssid

    def publish(self,phase,message,**extra):
        value={'job':self.job,'ssid':display_name(self.ssid),'phase':phase,'message':message,
               'pid':os.getpid(),'monotonic':time.monotonic(),**extra}
        atomic_file(RUNTIME/'status.json',json.dumps(value,ensure_ascii=False).encode())

    def original(self):return credential_bytes()

    def restart_dhcp(self):
        result=subprocess.run(['/usr/bin/sv','-w','6','restart','/run/frl/service/dhcp'],
                              stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=9)
        if result.returncode:raise WifiError('获取网络地址失败')

    def network_ready(self):
        sys.path.insert(0,'/usr/lib/frl/network') if '/usr/lib/frl/network' not in sys.path else None
        import frl_network
        return frl_network.managed_ipv4()

    def commit(self,old,new):
        if credential_bytes()!=old:raise WifiError('Wi-Fi 配置已被其他程序修改，已取消保存')
        folder=Path('/var/backups/frl/wifi')
        folder.mkdir(mode=0o700,exist_ok=True)
        info=folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:
            raise WifiError('Wi-Fi 备份目录权限异常')
        atomic_file(folder/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex+'.conf'),old)
        atomic_file(CONFIG,new)

    def wait(self,seconds):time.sleep(seconds)
    def now(self):return time.monotonic()


def connect(data,control,ops):
    """Never alter persistent credentials until association and fresh DHCP succeed."""
    data=request_data(data);ssid=bytes.fromhex(data['ssid_hex'])
    before=status(control)
    if before['connected'] and before['ssid_hex']==ssid.hex():
        ops.publish('connected','已连接此网络',ip=before['ip'],unchanged=True)
        return
    seen=parse_scan(control.request('SCAN_RESULTS'))
    if not any(r['ssid_hex']==ssid.hex() and r['security']==data['security'] for r in seen):
        raise WifiError('网络已不可见，请刷新后重试')
    original=ops.original()
    updated,psk,priority=profile(original,ssid,data['password'],data['security'])
    data['password']=''  # No plaintext password is put in commands, argv, files, or logs.
    changed=False;committed=False
    try:
        response=control.request('ADD_NETWORK')
        if not re.fullmatch(rb'[0-9]{1,9}',response):raise WifiError('无法创建连接配置')
        network=response.decode();changed=True
        control.request('SET_NETWORK '+network+' ssid '+ssid.hex())
        control.request('SET_NETWORK '+network+' key_mgmt '+('WPA-PSK' if psk else 'NONE'))
        if psk:
            control.request('SET_NETWORK '+network+' proto RSN')
            control.request('SET_NETWORK '+network+' psk '+psk)
        control.request('SET_NETWORK '+network+' priority '+str(priority))
        ops.publish('connecting','正在连接 Wi-Fi…')
        control.request('SELECT_NETWORK '+network)
        # Existing native supervisor tolerates 10 s of lost association.
        # Bound this PSK/open attempt to 7 s, then restore the original runtime.
        deadline=ops.now()+7
        while True:
            current=status(control)
            if current['connected'] and current['ssid_hex']==ssid.hex():break
            if ops.now()>=deadline:raise WifiError('连接失败，请检查密码或信号')
            ops.wait(.25)
        ops.publish('address','已连接，正在获取网络地址…')
        ops.restart_dhcp()  # Explicit renewal also covers very fast SSID changes.
        deadline=ops.now()+20
        while True:
            current=status(control)
            if current['connected'] and current['ssid_hex']==ssid.hex() and current['ip'] and ops.network_ready():break
            if ops.now()>=deadline:raise WifiError('未获得网络地址，已恢复原网络配置')
            ops.wait(.5)
        ops.commit(original,updated);committed=True
        ops.publish('connected','连接成功，已保存',ip=current['ip'])
    except BaseException:
        if changed and not committed:
            # No persisted credential changes occurred. Reconfigure from the existing
            # file; even if the supervisor restarted WPA, this restores known profiles.
            try:
                control.request('RECONFIGURE')
                ops.restart_dhcp()
            except Exception:
                pass  # Native supervision continues recovery from unchanged disk config.
        raise


def interrupted(*args):raise WifiError('连接已取消，正在恢复原网络配置')


def main():
    if os.geteuid()!=0 or Path('/proc/1/comm').read_text().strip()!='frl-init':return 77
    private_runtime()
    lock=os.open(RUNTIME/'operation.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return 75
        raw=sys.stdin.buffer.read(4097)
        if len(raw)>4096:raise WifiError('连接请求过长')
        data=request_data(json.loads(raw));raw=b''
        ops=Operations(data['job'],bytes.fromhex(data['ssid_hex']))
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,interrupted)
        try:
            with Control() as control:connect(data,control,ops)
        except WifiError as exc:
            ops.publish('failed',str(exc));return 1
        except Exception:
            ops.publish('failed','Wi-Fi 操作失败，原配置已保留');return 1
        finally:data['password']=''
        return 0
    except Exception:
        return 1  # Never print raw request/exception/credential content.
    finally:os.close(lock)


if __name__=='__main__':raise SystemExit(main())
