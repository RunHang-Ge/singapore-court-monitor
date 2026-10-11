"""Small, dependency-free Linux container memory diagnostics. Values are MiB."""
import json
import logging
from pathlib import Path

LOG = logging.getLogger('uvicorn.error')


def snapshot(cgroup='/sys/fs/cgroup', process='/proc/self/status'):
    root = Path(cgroup)
    result = {'container_mib': None, 'limit_mib': None, 'peak_mib': None,
              'python_mib': None, 'oom_kills': None}
    def number(path):
        try:
            value = int(path.read_text().strip())
            return value if 0 <= value < 2 ** 60 else None
        except (OSError, ValueError):
            return None
    for key, v2, v1 in [('container_mib', 'memory.current', 'memory.usage_in_bytes'),
                        ('limit_mib', 'memory.max', 'memory.limit_in_bytes'),
                        ('peak_mib', 'memory.peak', 'memory.max_usage_in_bytes')]:
        value = number(root / v2)
        if value is None:
            value = number(root / 'memory' / v1)
        if value is not None:
            result[key] = round(value / 1024 ** 2, 1)
    try:
        for line in (root / 'memory.events').read_text().splitlines():
            name, value = line.split()
            if name == 'oom_kill':
                result['oom_kills'] = int(value)
    except (OSError, ValueError):
        pass
    try:
        for line in Path(process).read_text().splitlines():
            if line.startswith('VmRSS:'):
                result['python_mib'] = round(int(line.split()[1]) / 1024, 1)
                break
    except (OSError, ValueError, IndexError):
        pass
    return result


def log_memory(phase, browser_active=False):
    LOG.info('memory phase=%s browser_active=%s values=%s',
             phase, browser_active, json.dumps(snapshot(), separators=(',', ':')))
