"""Validated display preferences; power daemon remains the sole backlight writer."""
import json
import math
import os
from pathlib import Path
import stat
import socket
import tempfile

CONFIG = Path('/etc/frl/display.json')
BRIGHTNESS_CONFIG = Path('/etc/frl/brightness.json')
DEFAULT_BRIGHTNESS = 100
BRIGHTNESS_MAX = 6600  # Runtime LED max_brightness / vendor panel-bl-max, not the old 25% cap.
DEFAULT_SECONDS = 60
OPTIONS = ((30, '30 秒'), (60, '1 分钟'), (120, '2 分钟'),
           (300, '5 分钟'), (600, '10 分钟'), (0, '永不'))
ALLOWED = frozenset(value for value, _ in OPTIONS)


def validate(value):
    if type(value) is not int or value not in ALLOWED:
        raise ValueError('invalid display timeout')
    return value


def label(seconds):
    return dict(OPTIONS)[validate(seconds)]


def read_config(path, field):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 or info.st_mode & 0o022:
            raise ValueError('unsafe display settings file')
        if path in (CONFIG, BRIGHTNESS_CONFIG) and info.st_uid != 0:
            raise ValueError('display settings must be root-owned')
        raw = os.read(fd, 1025)
        if len(raw) > 1024:
            raise ValueError('display settings too large')
        data = json.loads(raw)
        if type(data) is not dict or set(data) != {'version', field} or type(data['version']) is not int or data['version'] != 1:
            raise ValueError('invalid display settings schema')
        return data[field]
    finally:
        os.close(fd)


def load_settings(path=CONFIG):
    """Missing/invalid config has a finite safe default, never accidental never-off."""
    try:
        return validate(read_config(path, 'idle_off_seconds')), None
    except FileNotFoundError:
        return DEFAULT_SECONDS, None
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        return DEFAULT_SECONDS, '显示设置无效，已使用 1 分钟默认值'


def write_config(value, field, path):
    if path in (CONFIG, BRIGHTNESS_CONFIG) and os.geteuid() != 0:
        raise PermissionError('root settings writer required')
    if path.is_symlink():
        raise ValueError('settings symlink refused')
    parent = path.parent
    if path in (CONFIG, BRIGHTNESS_CONFIG):
        if parent.resolve() != parent or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
            raise ValueError('unsafe configuration directory')
    data = json.dumps({'version': 1, field: value}, indent=2).encode() + b'\n'
    fd, temporary = tempfile.mkstemp(prefix='.frl-display-', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_settings(seconds, path=CONFIG):
    write_config(validate(seconds), 'idle_off_seconds', path)


def validate_brightness(percent):
    if type(percent) not in (int, float) or not math.isfinite(percent) or not 1 <= percent <= 100:
        raise ValueError('invalid brightness percentage')
    return round(percent, 2)


def brightness_raw(percent):
    return int(validate_brightness(percent) * BRIGHTNESS_MAX / 100 + .5)


def load_brightness(path=BRIGHTNESS_CONFIG):
    try:
        return validate_brightness(read_config(path, 'brightness_percent')), None
    except FileNotFoundError:
        return DEFAULT_BRIGHTNESS, None
    except (OSError, ValueError, TypeError, UnicodeError):
        return DEFAULT_BRIGHTNESS, '亮度设置无效，已使用默认亮度'


def save_brightness(percent, path=BRIGHTNESS_CONFIG):
    write_config(validate_brightness(percent), 'brightness_percent', path)


def brightness_request(operation):
    """Root-only local power socket; previews do not write persistent storage."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.settimeout(.25)
        client.connect('/run/frl/power-terminal/control.sock')
        client.send(operation.encode('ascii'))
        result = json.loads(client.recv(4096))
    if not result.get('ok'):
        raise RuntimeError('亮度预览不可用，请稍后重试')


def preview_brightness(percent):
    brightness_request('brightness-preview ' + str(validate_brightness(percent)))


def clear_brightness_preview():
    brightness_request('brightness-clear')
