"""Recover only identity-verified WLAN children owned by a dead supervisor."""
import os,signal,time
from pathlib import Path

def reap_orphans(role,state):
    import frl_network as n
    records=[]
    for r in state.get('children',[]):
        if n.alive(r):records.append(r)
        elif n.group_members(r.get('group',-1)):
            raise n.Failure('orphan-descendants-without-verified-leader')
    if not records:return
    if role not in ('wpa','dhcp') or n.alive(state.get('owner',{})):
        raise n.Failure('prior-child-still-alive-no-duplicate-start')
    verified=[]
    for r in records:
        pid=r['pid'];proc=Path('/proc')/str(pid)
        args=(proc/'cmdline').read_bytes().split(b'\0')
        expected='/usr/sbin/wpa_supplicant' if role=='wpa' else n.BB
        if (proc.stat().st_uid!=0 or r.get('group')!=pid or r.get('session')!=pid or
                not os.path.samefile(proc/'exe',expected) or b'-i' not in args or
                args[args.index(b'-i')+1]!=b'wlan0' or
                (role=='dhcp' and b'udhcpc' not in args)):
            raise n.Failure('orphan-identity-not-qualified')
        verified.append(r)
    for sig in (signal.SIGTERM,signal.SIGKILL):
        for r in verified:
            current=n.identity(r['pid'])
            if current and current['start']!=r['start']:
                raise n.Failure('orphan-pid-reused')
            if n.alive(r) or n.group_members(r['group']):
                os.killpg(r['group'],sig)
        end=time.monotonic()+2
        while any(n.group_members(r['group']) for r in verified) and time.monotonic()<end:
            time.sleep(.05)
    if any(n.group_members(r['group']) for r in verified):
        raise n.Failure('orphan-cleanup-incomplete')
    print(role+': reclaimed verified orphan session',flush=True)

class AssociationRenewal:
    def __init__(self,supervisor,child):
        self.supervisor=supervisor;self.child=child
        self.was_connected=True;self.awaiting_generation=None

    def __call__(self):
        import frl_network as n
        s=self.supervisor
        connected=n.role_alive('wpa') and n.association()
        if not connected:
            self.was_connected=False
            return False
        if not self.was_connected:
            current=n.read_json(n.RUNTIME/'lease.json')
            self.awaiting_generation=int(current.get('generation',0))+1
            s.state.update(renew_pending=True,renew_requests=s.state.get('renew_requests',0)+1)
            s.save()
            os.kill(self.child.pid,signal.SIGUSR1)
            self.was_connected=True
        if self.awaiting_generation is not None:
            lease=n.read_json(n.RUNTIME/'lease.json')
            if int(lease.get('generation',0))<self.awaiting_generation:return False
            self.awaiting_generation=None
            s.state['renew_pending']=False;s.save()
        return n.managed_ipv4()
