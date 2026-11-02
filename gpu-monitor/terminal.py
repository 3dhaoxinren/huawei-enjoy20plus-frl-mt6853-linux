"""Read and type into the existing user tmux pane, never a privileged shell."""
import re
import subprocess
from pathlib import Path
import unicodedata

PREFIX = ['/usr/bin/chpst', '-u', 'frl:frl', '/usr/bin/env', 'HOME=/home/frl',
          'USER=frl', 'XDG_RUNTIME_DIR=/run/user/1000']
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
PALETTE = ['#192330', '#AF2337', '#218647', '#796000', '#174EA6', '#7540A0', '#147C86', '#536171']


def cells(value):
    return 0 if unicodedata.combining(value) else 2 if unicodedata.east_asian_width(value) in ('W', 'F') else 1


def spans(line):
    """Parse bounded tmux SGR captures, with a readable light-theme palette."""
    foreground, offset = 'text', 0
    for match in ANSI.finditer(line):
        if match.start() > offset:
            yield line[offset:match.start()], foreground
        if match.group().endswith('m'):
            codes = [int(x or 0) for x in match.group()[2:-1].split(';') if (x or '0').isdigit()]
            for code in codes:
                if code in (0, 39):
                    foreground = 'text'
                elif 30 <= code <= 37 or 90 <= code <= 97:
                    foreground = PALETTE[code % 10]
            # Extended 24/256-colour backgrounds are intentionally not executed as escapes.
        offset = match.end()
    if offset < len(line):
        yield line[offset:], foreground


class Terminal:
    def __init__(self, name='console'):
        if not re.fullmatch(r'console|gui-test-[a-zA-Z0-9-]{1,40}', name):
            raise ValueError('invalid session')
        self.name, self.pane = name, None
        self.rows, self.columns, self.scroll = 0, 56, 0
        self.lines, self.cursor = [], None
        self.pid = None

    def command(self, *args, check=True):
        result = subprocess.run([*PREFIX, '/usr/bin/tmux', '-L', 'frl-console', *args],
                                capture_output=True, text=True, timeout=3, check=check)
        return result.stdout

    def attach(self):
        path = str(Path(__file__).resolve().parent)
        code = f'import sys;sys.path.insert(0,{path!r});import session;print(session.prepare({self.name!r}))'
        self.pane = subprocess.check_output([*PREFIX, '/usr/bin/python3', '-B', '-c', code], text=True, timeout=5).strip()
        if not re.fullmatch(r'%[0-9]+', self.pane):
            raise RuntimeError('invalid tmux pane response')
        self.pid = int(self.command('display-message', '-p', '-t', self.pane, '#{pane_pid}').strip())

    def resize(self, rows):
        if self.pane is None:
            self.attach()
        if self.rows != rows:
            self.command('resize-window', '-t', self.name + ':0', '-x', str(self.columns), '-y', str(rows))
            self.rows = rows

    def capture(self):
        args = ['capture-pane', '-p', '-e', '-t', self.pane]
        if self.scroll:
            args.extend(['-S', str(-self.scroll), '-E', str(self.rows - self.scroll - 1)])
        self.lines = self.command(*args).splitlines()[:self.rows]
        state = self.command('display-message', '-p', '-t', self.pane, '#{cursor_x}|#{cursor_y}|#{cursor_flag}|#{history_size}').strip().split('|')
        self.cursor = (int(state[0]), int(state[1])) if len(state) == 4 and state[2] == '1' and not self.scroll else None
        self.history = int(state[3]) if len(state) == 4 else 0

    def send(self, data):
        if not isinstance(data, bytes) or len(data) > 256:
            raise ValueError('bounded UTF-8 key bytes required')
        if data:
            self.command('send-keys', '-t', self.pane, '-H', *(f'{b:02x}' for b in data))
            self.scroll = 0
