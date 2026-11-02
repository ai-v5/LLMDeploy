import os
from pathlib import Path
import subprocess
import unittest


def synthetic_caddy_owns_listener(process, port):
    """Check ownership before sending requests to a released test port."""
    if process.poll() is not None:
        return False
    if os.name == 'nt':
        result = subprocess.run(['netstat', '-ano', '-p', 'tcp'], capture_output=True,
                                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            return False
        listeners = []
        for line in result.stdout.decode('ascii', errors='replace').splitlines():
            fields = line.split()
            if len(fields) != 5 or fields[0] != 'TCP' or fields[3] != 'LISTENING':
                continue
            address, separator, candidate_port = fields[1].rpartition(':')
            if separator and candidate_port.isdigit() and int(candidate_port) == port:
                listeners.append((address, fields[4]))
        return listeners == [('127.0.0.1', str(process.pid))] and process.poll() is None
    proc = Path('/proc')
    if not (proc/'net/tcp').is_file():
        raise unittest.SkipTest('synthetic Caddy listener ownership cannot be verified')
    listeners = []
    for name in ('tcp', 'tcp6'):
        path = proc/'net'/name
        if not path.is_file():
            continue
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            address, candidate_port = fields[1].split(':')
            if int(candidate_port, 16) == port and fields[3] == '0A':
                listeners.append((name, address, fields[9]))
    if len(listeners) != 1 or listeners[0][:2] != ('tcp', '0100007F'):
        return False
    try:
        descriptors = list((proc/str(process.pid)/'fd').iterdir())
    except OSError:
        return False
    for descriptor in descriptors:
        try:
            if os.readlink(descriptor) == f'socket:[{listeners[0][2]}]':
                return process.poll() is None
        except OSError:
            continue
    return False
