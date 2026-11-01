"""Read the bounded, public numeric subset of local backend metrics."""
from http.client import HTTPConnection, HTTPException
import math
import time

METRICS = {
    'deepinfer:requests_running': 'running',
    'deepinfer:requests_waiting': 'waiting',
    'deepinfer:workers_alive': 'workers_alive',
    'deepinfer:admission_ready': 'admission_ready',
    'deepinfer:prompt_tokens_total': 'input_total',
    'deepinfer:prompt_cache_hit_tokens_total': 'cached_total',
    'deepinfer:prompt_cache_hit_requests_total': 'cache_hit_requests_total',
}


def read_backend(port):
    result = {'port': port, 'sampled_at': time.time(), 'available': False}
    connection = HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        connection.request('GET', '/metrics')
        response = connection.getresponse()
        if response.status != 200:
            return result
        payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            return result
        values = {}
        for line in payload.decode('utf-8').splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[0] not in METRICS:
                continue
            name = METRICS[parts[0]]
            value = float(parts[1])
            if name in values or not math.isfinite(value) or value < 0:
                return result
            values[name] = value
        if set(values) != set(METRICS.values()) or values['admission_ready'] not in (0, 1):
            return result
        result.update(values, available=True)
        result['hit_rate'] = (values['cached_total'] / values['input_total']
                              if values['input_total'] and values['cached_total'] <= values['input_total'] else None)
        return result
    except (OSError, HTTPException, UnicodeError, ValueError, OverflowError):
        return result
    finally:
        connection.close()
