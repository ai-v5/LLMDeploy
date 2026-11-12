"""Keep the native API/agent parent alive when the container has no init."""
from pathlib import Path
import signal
import subprocess
import sys


def main():
    entry = Path(sys.argv[1]).resolve()
    if entry != Path(__file__).resolve().with_name('runtime_entry.py'):
        raise ValueError('SUPERVISOR_REQUIRES_FIXED_RUNTIME_ENTRY')
    child = subprocess.Popen([sys.executable, str(entry), *sys.argv[2:]])

    def forward(number, frame):
        if child.poll() is None:
            try:
                child.send_signal(number)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    result = child.wait()
    return result if result >= 0 else 128 - result


if __name__ == '__main__':
    raise SystemExit(main())
