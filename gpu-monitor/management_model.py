"""Single read worker/mailbox plus one detached mutation; no blocking UI I/O."""
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid
import management_backend as backend


class ManagementModel:
    def __init__(self):
        self.power = None;self.apps = {'rows': [], 'stamp': 0};self.logs = []
        self.error = None;self.message = '';self.result = None
        self.loading = False;self.busy = False;self.worker = None;self.job = None
        self.next_refresh = 0;self.next_status = 0;self.started = 0
        self._mail = None;self._lock = threading.Lock();self._request = None
        self._retired = []

    def refresh(self, view, name=None):
        if self.loading:return False
        self.loading = True;self._request = (view, name)
        def read():
            try:
                value = backend.power_snapshot() if view == 'power' else backend.app_logs(name) if view == 'logs' else backend.app_snapshot()
                packet = (view, name, value, None)
            except Exception as exc:packet = (view, name, None, backend.clean(exc, 120))
            with self._lock:self._mail = packet
        threading.Thread(target=read, daemon=True, name='management-read').start()
        return True

    def submit(self, request):
        if self.busy:raise backend.ManagementError('已有操作正在执行')
        self.job = uuid.uuid4().hex
        request = {**request, 'job': self.job}
        self.worker = subprocess.Popen(['/usr/bin/python3', '-I', str(Path(__file__).with_name('management_worker.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
        # nosec path-traversal: data is a bounded pipe request, not a path; worker validates fields.
        try:self.worker.stdin.write(json.dumps(request).encode());self.worker.stdin.close()
        except Exception:
            self.worker.terminate();raise
        self.busy = True;self.started = time.monotonic();self.message = '正在执行，请勿重复操作'
        self.next_status = 0;self.error = None

    def poll(self, view=None, name=None):
        changed = False;now = time.monotonic()
        self._retired = [child for child in self._retired if child.poll() is None]
        with self._lock:packet, self._mail = self._mail, None
        if packet:
            which, target, value, error = packet
            self.loading = False;self.error = error;changed = True
            if not error:
                if which == 'power':self.power = value
                elif which == 'apps':self.apps = value
                elif (view, name) == ('logs', target):self.logs = value
            self.next_refresh = now+5 if self._request == (view, name) else 0
        if view and not self.loading and now >= self.next_refresh:
            self.refresh(view, name);self.next_refresh = now+5
        # Small trusted status file only; CLI and application log I/O stay on the read worker.
        if now >= self.next_status:
            self.next_status = now+.3
            try:
                result = backend.job_status()
                if result and (not self.job or result['job'] == self.job):
                    if result != self.result:self.result = result;self.message = result['message'];changed = True
                    self.busy = result['phase'] == 'running' and now-result['started'] < 50
                    if result['phase'] == 'running' and not self.busy:
                        self.message = '操作状态未知，请刷新核对；不会自动重试'
            except (OSError, ValueError, backend.ManagementError):pass
            if self.worker and self.worker.poll() is not None and (not self.result or self.result['job'] != self.job) and now-self.started > 1:
                self.busy = False;self.worker = None;self.message = '操作未取得结果，请刷新核对';changed = True
            if self.result and self.result['job'] == self.job and self.result['phase'] != 'running':
                self.job = None
                if self.worker:self._retired.append(self.worker)
                self.worker = None;self.next_refresh = 0
        return changed
