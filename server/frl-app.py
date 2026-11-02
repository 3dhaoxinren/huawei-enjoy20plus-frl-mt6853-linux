#!/usr/bin/python3 -I
"""Register user applications with runit restart and bounded rotating logs."""
import argparse,json,os,pwd,re,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,'/usr/lib/frl')
from server_common import atomic,guard

CONFIG=Path('/etc/frl/apps');LIVE=Path('/run/frl/apps')

def valid_name(name):
    if not re.fullmatch('[a-z][a-z0-9-]{0,31}',name):raise ValueError('invalid application name')
    return name

def read(name):
    path=CONFIG/(valid_name(name)+'.json');s=path.lstat()
    if not path.is_file() or path.is_symlink() or s.st_uid!=0 or s.st_mode & 0o022:raise RuntimeError('untrusted application configuration')
    c=json.loads(path.read_text())
    if set(c)!={'name','enabled','cwd','argv'} or c['name']!=name or type(c['enabled']) is not bool:raise ValueError('invalid application configuration')
    if not isinstance(c['argv'],list) or not c['argv'] or any(not isinstance(v,str) or '\0' in v for v in c['argv']):raise ValueError('invalid command arguments')
    if not c['argv'][0].startswith('/'):raise ValueError('executable must be absolute')
    cwd=Path(c['cwd']).resolve(strict=True)
    if not cwd.is_dir() or not cwd.is_relative_to('/srv/frl'):raise ValueError('application cwd must be under /srv/frl')
    return c

def save(c):atomic(CONFIG/(c['name']+'.json'),(json.dumps(c,indent=2)+'\n').encode())

def prepare(c):
    name=c['name'];path=LIVE/name;path.mkdir(parents=True,exist_ok=True)
    log=Path('/var/log/frl/apps')/name;log.mkdir(parents=True,exist_ok=True)
    atomic(log/'config',b's1048576\nn3\n',0o644)
    (path/'log').mkdir(exist_ok=True)
    atomic(path/'run',('#!/bin/sh\nexec 2>&1\nsleep 2\nexec /usr/local/sbin/frl-app run '+name+'\n').encode(),0o755)
    atomic(path/'log/run',('#!/bin/sh\nexec /usr/bin/svlogd -tt '+str(log)+'\n').encode(),0o755)
    if c['enabled']: (path/'down').unlink(missing_ok=True)
    else:atomic(path/'down',b'',0o644)

def boot():
    CONFIG.mkdir(parents=True,exist_ok=True);LIVE.mkdir(mode=0o755,exist_ok=True)
    for path in sorted(CONFIG.glob('*.json')):prepare(read(path.stem))

def supervise():
    boot();stopping=[False]
    def stop(*args):stopping[0]=True
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    child=subprocess.Popen(['/usr/bin/runsvdir','-P',str(LIVE)])
    try:
        while not stopping[0]:
            if child.poll() is not None:raise RuntimeError('application supervisor exited')
            time.sleep(.5)
    finally:
        errors={}
        try:
            for path in sorted(LIVE.iterdir()):
                if not path.is_dir():continue
                try:subprocess.run(['/usr/bin/sv','-w','4','exit',str(path)],timeout=6,check=True)
                except Exception as exc:errors[path.name]=str(exc)
        finally:
            child.terminate()
            try:child.wait(timeout=4)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        if errors:raise RuntimeError('application shutdown incomplete: '+json.dumps(errors))

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    for action in ('boot','supervise','list'):sub.add_parser(action)
    for action in ('enable','disable','restart','run','logs'):
        sub.add_parser(action).add_argument('name')
    add=sub.add_parser('add');add.add_argument('name');add.add_argument('--cwd',required=True);add.add_argument('--command',nargs=argparse.REMAINDER,required=True)
    args=p.parse_args();guard()
    if args.action=='boot':boot();return
    if args.action=='supervise':supervise();return
    if args.action=='list':
        result=[]
        for path in sorted(CONFIG.glob('*.json')):
            c=read(path.stem);live=LIVE/c['name']
            state=subprocess.run(['/usr/bin/sv','status',str(live)],capture_output=True,text=True,timeout=3).stdout.strip()
            result.append({'name':c['name'],'enabled':c['enabled'],'status':state,'cwd':c['cwd']})
        print(json.dumps(result));return
    name=valid_name(args.name)
    if args.action=='add':
        path=CONFIG/(name+'.json')
        if path.exists():raise RuntimeError('application already exists')
        CONFIG.mkdir(parents=True,exist_ok=True)
        c={'name':name,'enabled':False,'cwd':args.cwd,'argv':args.command};save(c)
        try:c=read(name)
        except Exception:path.unlink();raise
        prepare(c);print(json.dumps({'added':name,'enabled':False}));return
    c=read(name)
    if args.action=='run':
        if not c['enabled']:raise RuntimeError('application is disabled')
        os.chdir(c['cwd'])
        env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/srv/frl','USER':'frl-app','LOGNAME':'frl-app','LANG':'C.UTF-8','TZ':'Asia/Shanghai','PYTHONUNBUFFERED':'1'}
        account=pwd.getpwnam('frl-app')
        if account.pw_uid==0 or account.pw_gid==0:raise RuntimeError('application identity must be unprivileged')
        os.execve('/usr/bin/setpriv',['setpriv','--reuid',str(account.pw_uid),'--regid',str(account.pw_gid),'--clear-groups','--no-new-privs','--',*c['argv']],env)
    if args.action=='logs':
        os.execv('/usr/bin/tail',['tail','-n','100',str(Path('/var/log/frl/apps')/name/'current')])
    if args.action in ('enable','disable'):c['enabled']=args.action=='enable';save(c)
    prepare(c);live=LIVE/name
    for _ in range(30):
        if (live/'supervise/ok').exists():break
        time.sleep(.25)
    verb='restart' if args.action=='restart' else 'up' if c['enabled'] else 'down'
    subprocess.run(['/usr/bin/sv','-w','12',verb,str(live)],check=True,timeout=16)
    print(json.dumps({'name':name,'enabled':c['enabled']}))

if __name__=='__main__':
    try:main()
    except Exception as exc:print('frl-app: '+str(exc),file=sys.stderr);sys.exit(1)
