"""Detached bounded local job; survives display power-off, never retries a mutation."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from management_backend import run_job


def main():
    if os.geteuid() != 0 or Path('/proc/1/comm').read_text().strip() != 'frl-init':
        raise RuntimeError('native device root required')
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:raise ValueError('request too large')
    run_job(json.loads(raw))


if __name__ == '__main__':
    main()
