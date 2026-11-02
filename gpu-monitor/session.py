"""Attach to an existing tmux shell without replacing it or typing into it."""
import os
import fcntl
import stat
import re
import subprocess
import sys

TMUX = ["/usr/bin/tmux", "-L", "frl-console"]


def command(*args, check=True):
    return subprocess.run([*TMUX, *args], check=check, capture_output=True, text=True, timeout=5)


def prepare(name="console"):
    if not re.fullmatch(r"console|gui-test-[a-zA-Z0-9-]{1,40}", name):
        raise ValueError("invalid session name")
    if os.getuid() != 1000:
        raise RuntimeError("run as frl, not root")
    lock = os.open('/run/user/1000/frl-monitor-session-' + name + '.lock',
                   os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("untrusted session lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        return prepare_locked(name)
    finally:
        os.close(lock)


def prepare_locked(name):
    if command("has-session", "-t", "=" + name, check=False).returncode:
        shell = "/usr/lib/frl/terminal-shell.sh" if name == "console" else "/bin/bash --noprofile --norc"
        command("new-session", "-d", "-s", name, "-x", "52", "-y", "24", shell)
    panes = command("list-panes", "-t", name + ":0", "-F", "#{pane_id}|#{pane_start_command}|#{pane_dead}").stdout.splitlines()
    target = next((p.split("|", 1)[0] for p in panes if "server-monitor-run.sh" not in p and p.endswith("|0")), None)
    if not target:
        raise RuntimeError("no live shell; refusing to replace a dead/user pane")
    command("select-pane", "-t", target)
    zoomed = command("display-message", "-p", "-t", target, "#{window_zoomed_flag}").stdout.strip()
    if len(panes) > 1 and zoomed != "1":
        command("resize-pane", "-Z", "-t", target)
    command("set-option", "-t", name, "status", "off")
    command("set-option", "-w", "-t", name + ":0", "pane-border-status", "off")
    return target


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "console"
    prepare(name)
    os.execv(TMUX[0], [*TMUX, "attach-session", "-t", "=" + name])
