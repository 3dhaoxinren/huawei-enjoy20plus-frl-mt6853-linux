#!/usr/bin/env python3
"""FRL release-material exporter. Standard library + OpenSSH; no phone writes."""
import argparse
import fnmatch
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time

PREFIX = 'FRL_EXPORT_REPORT '
TOPS = ('etc', 'usr', 'opt', 'var', 'boot', 'bin', 'sbin', 'lib', 'lib64')
EXCLUDES = (
    'etc/shadow', 'etc/shadow-', 'etc/gshadow', 'etc/gshadow-',
    'etc/ssh/ssh_host_*', 'etc/frl/ssh/authorized_keys',
    'etc/wpa_supplicant/*.conf', 'etc/NetworkManager/system-connections',
    'etc/netplan', 'etc/network/interfaces', 'etc/network/interfaces.d',
    'etc/frl/network/wlan-mac', 'etc/frl/apps', 'etc/machine-id',
    'var/log', 'var/cache', 'var/tmp', 'var/backups', 'var/spool',
    'var/lib/dbus/machine-id', 'var/lib/systemd/random-seed',
    'var/lib/NetworkManager', 'var/lib/connman', 'var/lib/dhcp',
    'var/lib/chrony', 'var/lib/private', 'var/lib/docker', 'var/lib/containers',
    'var/lib/mysql', 'var/lib/postgresql', 'var/lib/redis',
)
ALIASES = {'bin': 'usr/bin', 'sbin': 'usr/sbin', 'lib': 'usr/lib', 'lib64': 'usr/lib64'}

REMOTE = r'''
import os, sys, json, hashlib, subprocess, stat
from pathlib import Path, PurePosixPath

def sha(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        while True:
            b=f.read(1048576)
            if not b:break
            h.update(b)
    return h.hexdigest()

def emit(value):
    sys.stderr.write(PREFIX+json.dumps(value,ensure_ascii=True)+'\n');sys.stderr.flush()

def state():
    p=Path('/etc/frl/root-payload.sha256');raw=p.read_text()
    records={}
    for line in raw.splitlines():
        digest,name=line.split('  ',1)
        if (len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)
            or name.startswith('/') or '..' in PurePosixPath(name).parts or name in records):
            raise RuntimeError('Invalid payload manifest')
        if sha('/'+name)!=digest:raise RuntimeError('Payload mismatch: '+name)
        records[name]=digest
    return {'payload':records,'payload_manifest_sha256':sha(p),
            'dpkg_status_sha256':sha('/var/lib/dpkg/status')}

def optional_text(name, limit=2000000):
    p=Path(name)
    try:
        with p.open('rb') as f:raw=f.read(limit+1)
        if len(raw)>limit:return {'error':'too-large'}
        return raw.decode('utf-8',errors='replace')
    except OSError as e:return {'error':type(e).__name__}

if os.geteuid()!=0:raise RuntimeError('sudo without a password is required')
if os.uname().machine!='aarch64':raise RuntimeError('Not an ARM64 device')
if Path('/proc/1/comm').read_text().strip()!='frl-init':raise RuntimeError('Not the native FRL system')
if Path('/sys/firmware/devicetree/base/compatible').read_bytes()!=b'mediatek,MT6853\0':
    raise RuntimeError('Not the expected MT6853 compatible value')
before=state()
version=subprocess.run(['tar','--version'],capture_output=True,text=True,check=True).stdout.splitlines()[0]
if 'GNU tar' not in version:raise RuntimeError('GNU tar required to preserve Linux metadata')
packages=subprocess.run(['dpkg-query','-W','-f=${binary:Package}\t${Version}\t${Architecture}\n'],capture_output=True,text=True,check=True).stdout
dirs={}
for name in ('/root','/home','/home/frl','/srv','/srv/frl','/srv/frl/data','/tmp','/run','/dev','/proc','/sys'):
    try:
        s=Path(name).lstat();dirs[name]={'mode':oct(stat.S_IMODE(s.st_mode)),'uid':s.st_uid,'gid':s.st_gid}
    except OSError:pass
parts=[]
for p in sorted(Path('/sys/class/block').glob('*')):
    try:
        info=dict(line.split('=',1) for line in (p/'uevent').read_text().splitlines() if '=' in line)
        if 'PARTNAME' in info:parts.append({'name':info['PARTNAME'],'node':info.get('DEVNAME'),'sectors':int((p/'size').read_text())})
    except (OSError,ValueError):pass
info={'phase':'before','state':before,'kernel':os.uname().release,'machine':os.uname().machine,
      'os_release':optional_text('/etc/os-release'),'packages_tsv':packages,'tar_version':version,
      'mounts':optional_text('/proc/mounts'),'mountinfo':optional_text('/proc/self/mountinfo'),
      'partition_layout':parts,'empty_directory_metadata':dirs,
      'deployment_flag':optional_text('/run/frl/storage/frl-linux/v15-deployed.flag'),
      'stock_build_properties':{p:optional_text(p) for p in (
          '/run/frl/stock/vendor/build.prop','/run/frl/stock/system/system/build.prop')},
      'excluded_patterns':EXCLUDES,'live_export_not_cold_snapshot':True}
config=Path('/proc/config.gz')
if config.is_file():
    import base64
    with config.open('rb') as f:raw=f.read(1048577)
    if len(raw)<=1048576:info['kernel_config_gz_base64']=base64.b64encode(raw).decode('ascii')
emit(info)
if CHECK_ONLY:sys.exit(0)
rootdev=Path('/').stat().st_dev
tops=[]
for name in TOPS:
    p=Path('/')/name
    if p.exists() or p.is_symlink():
        if p.lstat().st_dev!=rootdev:raise RuntimeError('Top-level mount needs manual review: '+name)
        tops.append(name)
command=['tar','--one-file-system','--numeric-owner','--acls','--xattrs','--xattrs-include=*',
         '--sparse','--format=pax','-C','/','-czf','-']
command+=['--exclude='+p for p in EXCLUDES]
command+=tops
done=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=sys.stdout.buffer,stderr=sys.stderr.buffer)
if done.returncode:raise RuntimeError('tar exited '+str(done.returncode)+'; export is incomplete')
after=state()
if after!=before:raise RuntimeError('Payload or package state changed during export; retry while idle')
emit({'phase':'after','state':after,'tar_exit':0,'stable_payload_and_package_state':True})
'''


def clean_name(name):
    while name.startswith('./'):
        name = name[2:]
    name = name.rstrip('/')
    if not name or name.startswith('/') or '..' in PurePosixPath(name).parts or '\\' in name:
        raise ValueError('Unsafe archive path')
    return name


def canonical(name):
    first, sep, rest = name.partition('/')
    return ALIASES[first] + sep + rest if first in ALIASES and sep else name


def excluded(name):
    name = clean_name(name)
    return any(fnmatch.fnmatchcase(name, p) or name.startswith(p+'/') for p in EXCLUDES)


def ssh_command(host, user='frl', port=22, identity=None, ssh=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:\[\]-]*', host):
        raise ValueError('IP / SSH host alias contains unsupported characters')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', user) or not 1 <= int(port) <= 65535:
        raise ValueError('Invalid SSH user or port')
    binary = ssh or shutil.which('ssh')
    if not binary:
        raise RuntimeError('OpenSSH client not found; install/enable the SSH client first')
    cmd = [binary, '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
           '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
           '-o', 'ServerAliveCountMax=3', '-p', str(port)]
    if identity:
        key = Path(identity).expanduser().resolve(strict=True)
        if not key.is_file():
            raise ValueError('SSH identity must be a regular file')
        cmd += ['-o', 'IdentitiesOnly=yes', '-i', str(key)]
    return cmd + [user+'@'+host, 'sudo -n /usr/bin/python3 -B -']


def remote_source(check_only=False):
    settings = 'PREFIX='+repr(PREFIX)+'\nTOPS='+repr(TOPS)+'\nEXCLUDES='+repr(EXCLUDES)
    return (settings+'\nCHECK_ONLY='+repr(check_only)+'\n'+REMOTE).encode('utf-8')


def reports(log):
    found = {}
    for line in Path(log).read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith(PREFIX):
            obj = json.loads(line[len(PREFIX):])
            phase = obj['phase']
            if phase in found:
                raise ValueError('Duplicate remote report')
            found[phase] = obj
    return found


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def verify_archive(path, payload, progress=lambda text: None, cancel=None):
    expected = {canonical(clean_name(k)): v for k, v in payload.items()}
    if len(expected) != len(payload):
        raise ValueError('Ambiguous payload aliases')
    hashes, links, seen = {}, {}, set()
    count = 0
    with gzip.open(path, 'rb') as compressed:
        with tarfile.open(fileobj=compressed, mode='r|') as archive:
            for member in archive:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError('Export cancelled during verification')
                name = clean_name(member.name)
                if name in seen or name.split('/')[0] not in TOPS or excluded(name):
                    raise ValueError('Duplicate / unexpected / excluded member: '+name)
                seen.add(name); count += 1
                key = canonical(name)
                if member.islnk():
                    links[key] = canonical(clean_name(member.linkname))
                elif member.isfile():
                    f = archive.extractfile(member)
                    h = hashlib.sha256()
                    for block in iter(lambda: f.read(1024*1024), b''):
                        if cancel is not None and cancel.is_set():
                            raise RuntimeError('Export cancelled during verification')
                        h.update(block)
                    hashes[key] = h.hexdigest()
                elif not (member.isdir() or member.issym()):
                    raise ValueError('Unexpected special file: '+name)
                if count % 2000 == 0:
                    progress('校验归档结构：'+str(count)+' 个条目')
        # Read gzip trailers too: a valid tar header alone is not a complete export.
        while compressed.read(1024*1024):
            pass
    def resolve(key, chain=()):
        if key in chain:
            raise ValueError('Hardlink cycle')
        if key in hashes:
            return hashes[key]
        if key in links:
            return resolve(links[key], chain+(key,))
        return None
    missing = [key for key, want in expected.items() if resolve(key) != want]
    if missing:
        raise ValueError('Archive does not reproduce tracked payload files: '+', '.join(missing[:12]))
    return {'archive_members': count, 'payload_files_verified': len(expected), 'gzip_and_tar_verified': True}


def stop_process(p):
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()


def collect(host, output, user='frl', port=22, identity=None, check_only=False,
            progress=lambda text: None, cancel=None):
    cancel = cancel or threading.Event()
    command = ssh_command(host, user, port, identity)
    parent = Path(output).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(parent).free < (50*1024*1024 if check_only else 8*1024**3):
        raise RuntimeError('Export requires at least 8 GiB free on the computer (50 MiB for connection check)')
    folder = parent / (time.strftime('frl-private-export-%Y%m%d-%H%M%S')+'-'+os.urandom(3).hex())
    folder.mkdir(mode=0o700)
    partial = folder/'system-files.private.tar.gz.partial'
    log = folder/'export.private.log'
    receipt = {'complete': False, 'check_only': check_only, 'privacy_review_required': True}
    p = None
    start = time.monotonic()
    try:
        progress('正在验证 SSH、机型、原生系统和现有 payload……')
        with partial.open('xb') as data, log.open('xb') as error:
            kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=data, stderr=error, **kwargs)
            p.stdin.write(remote_source(check_only)); p.stdin.close()
            last = 0
            while p.poll() is None:
                if cancel.is_set():
                    raise RuntimeError('Export cancelled')
                now = time.monotonic()
                if now-last >= 1:
                    progress(f'接收中：{partial.stat().st_size/1024**2:.1f} MiB · {int(now-start)} 秒')
                    last = now
                time.sleep(.2)
            receipt['ssh_exit'] = p.returncode
        if p.returncode:
            raw = log.read_text(encoding='utf-8', errors='replace').splitlines()
            errors = [s for s in raw if not s.startswith(PREFIX)]
            raise RuntimeError('SSH / export failed.\n'+'\n'.join(errors[-12:])[-1800:])
        if cancel.is_set():
            raise RuntimeError('Export cancelled')
        meta = reports(log)
        if 'before' not in meta:
            raise RuntimeError('Missing authenticated device report')
        if check_only:
            receipt.update(complete=True, connection_verified=True,
                           payload_files_verified=len(meta['before']['state']['payload']))
            partial.unlink()
        else:
            if 'after' not in meta or meta['before']['state'] != meta['after']['state']:
                raise RuntimeError('Missing / inconsistent final report; not a complete export')
            progress('正在校验 gzip、归档路径及每个受跟踪的 payload 文件……')
            receipt.update(verify_archive(partial, meta['before']['state']['payload'], progress, cancel))
            if cancel.is_set():
                raise RuntimeError('Export cancelled')
            archive = folder/'system-files.private.tar.gz'
            partial.rename(archive)
            receipt.update(complete=True, archive=archive.name, bytes=archive.stat().st_size,
                           sha256=digest(archive), live_export_not_cold_snapshot=True)
        (folder/'device-info.private.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        (folder/'DO-NOT-PUBLISH.txt').write_text(
            'PRIVATE RELEASE MATERIALS. Not a flashable image. Do not upload to a public repository.\n'
            'Common credentials and personal directories were excluded, but remaining /etc, /opt and /var/lib require a separate privacy review.\n'
            'This is a live file-level export, not a consistent cold backup of every mutable file.\n', encoding='utf-8')
        progress('连接检查通过' if check_only else '导出完成；请私下提供整个输出文件夹，不要公开上传')
    except BaseException as exc:
        receipt.update(complete=False, error=str(exc))
        raise
    finally:
        stop_process(p)
        receipt['elapsed_seconds'] = round(time.monotonic()-start, 2)
        (folder/'result.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return folder


class Window:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        self.root, self.ttk = root, ttk
        self.filedialog, self.messagebox = filedialog, messagebox
        self.events = queue.Queue(); self.cancel = threading.Event(); self.running = False
        root.title('华为畅享 20 Plus · 系统导出工具')
        root.geometry('820x550'); root.minsize(720, 500)
        frame = ttk.Frame(root, padding=20); frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text='导出当前可用系统，供净化和重建镜像', font=('Microsoft YaHei UI', 15, 'bold')).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 8))
        ttk.Label(frame, text='不刷写手机 · 不导出整块 userdata · 默认排除常见密钥与个人目录').grid(row=1, column=0, columnspan=3, sticky='w', pady=(0, 18))
        self.host = tk.StringVar(); self.user = tk.StringVar(value='frl')
        self.port = tk.StringVar(value='22'); self.key = tk.StringVar()
        self.output = tk.StringVar(value=str(Path.home()/'Downloads'/'FRL-export'))
        for row, label, var in ((2,'手机 IP / SSH 别名',self.host),(3,'SSH 用户',self.user),(4,'SSH 端口',self.port),(5,'SSH 私钥（可选）',self.key),(6,'电脑保存目录',self.output)):
            ttk.Label(frame, text=label).grid(row=row,column=0,sticky='w',padx=(0,12),pady=6)
            ttk.Entry(frame, textvariable=var).grid(row=row,column=1,sticky='ew',pady=6)
        ttk.Button(frame,text='选择文件',command=self.choose_key).grid(row=5,column=2,padx=(8,0))
        ttk.Button(frame,text='选择目录',command=self.choose_output).grid(row=6,column=2,padx=(8,0))
        ttk.Label(frame,text='需要现有 SSH 公钥登录和免密码 sudo。首次连接请先在终端核对主机指纹。\n导出时请勿安装软件或修改系统配置；输出至少预留 8 GiB。',wraplength=730).grid(row=7,column=0,columnspan=3,sticky='w',pady=14)
        actions=ttk.Frame(frame); actions.grid(row=8,column=0,columnspan=3,sticky='w')
        self.check=ttk.Button(actions,text='检查连接',command=lambda:self.start(True));self.check.pack(side='left',padx=(0,8))
        self.export=ttk.Button(actions,text='开始导出',command=lambda:self.start(False));self.export.pack(side='left',padx=(0,8))
        self.stop=ttk.Button(actions,text='取消',command=self.cancel.set,state='disabled');self.stop.pack(side='left')
        self.progress=ttk.Progressbar(frame,mode='indeterminate');self.progress.grid(row=9,column=0,columnspan=3,sticky='ew',pady=16)
        self.status=tk.StringVar(value='尚未连接手机。导出结果是私有材料，不是可直接刷入的镜像。')
        ttk.Label(frame,textvariable=self.status,wraplength=730).grid(row=10,column=0,columnspan=3,sticky='w')
        root.protocol('WM_DELETE_WINDOW',self.close)
        self.timer=root.after(150,self.poll)

    def choose_key(self):
        path=self.filedialog.askopenfilename(title='选择 SSH 私钥（只在本机使用，不会上传）')
        if path:self.key.set(path)

    def choose_output(self):
        path=self.filedialog.askdirectory()
        if path:self.output.set(path)

    def start(self, check):
        if self.running:return
        try:
            args=(self.host.get().strip(),self.output.get(),self.user.get().strip(),int(self.port.get()),self.key.get().strip() or None)
            ssh_command(args[0],args[2],args[3],args[4])
        except Exception as e:
            self.messagebox.showerror('配置不完整',str(e));return
        self.running=True;self.cancel.clear();self.check.config(state='disabled');self.export.config(state='disabled');self.stop.config(state='normal');self.progress.start(12)
        def worker():
            try:
                folder=collect(*args,check_only=check,progress=lambda text:self.events.put(('progress',text)),cancel=self.cancel)
                self.events.put(('done',str(folder)))
            except Exception as e:self.events.put(('error',str(e)))
        threading.Thread(target=worker,daemon=True).start()

    def poll(self):
        try:
            while True:
                kind,text=self.events.get_nowait()
                if kind=='progress':self.status.set(text);continue
                self.running=False;self.progress.stop();self.check.config(state='normal');self.export.config(state='normal');self.stop.config(state='disabled')
                if kind=='done':self.status.set('完成。保存位置：'+text);self.messagebox.showinfo('完成',text+'\n\n请私下传送，不要直接公开上传。')
                else:self.status.set('未完成：'+text[:350]);self.messagebox.showerror('未完成',text)
        except queue.Empty:pass
        self.timer=self.root.after(150,self.poll)

    def close(self):
        if self.running:
            self.cancel.set();self.status.set('正在取消，请等任务结束后再关闭窗口。');return
        self.root.after_cancel(self.timer);self.root.destroy()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gui',action='store_true');p.add_argument('--host')
    p.add_argument('--user',default='frl');p.add_argument('--port',type=int,default=22)
    p.add_argument('--identity');p.add_argument('--output',default=str(Path.home()/'Downloads'/'FRL-export'))
    p.add_argument('--check-only',action='store_true')
    a=p.parse_args(argv)
    if a.gui or not a.host:
        import tkinter as tk
        root=tk.Tk();Window(root);root.mainloop();return
    folder=collect(a.host,a.output,a.user,a.port,a.identity,a.check_only,progress=lambda text:print(text,flush=True))
    print('OUTPUT='+str(folder))


if __name__=='__main__':
    try:main()
    except (Exception, KeyboardInterrupt) as e:
        print('FAILED: '+str(e),file=sys.stderr);sys.exit(1)
