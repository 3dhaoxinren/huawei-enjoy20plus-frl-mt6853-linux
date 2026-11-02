"""Run the current offline monitor suite without touching device services."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
env = {
    **os.environ,
    'PYTHONDONTWRITEBYTECODE': '1',
    'PYTHONPATH': os.pathsep.join((str(root/'gpu-monitor'), str(root/'server'))),
    'FRL_POWER_TEST_PATH': str(root/'power-terminal/power-terminal.py'),
}
raise SystemExit(subprocess.run(
    [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(root/'gpu-monitor/tests'), '-v'],
    cwd=root, env=env,
).returncode)
