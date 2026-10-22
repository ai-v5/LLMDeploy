"""Explicit short request to a local native API, without credentials or user data."""
import argparse
import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def check(base, *, request=urlopen):
    url = urlsplit(base)
    if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
            or url.username or url.password or url.query or url.fragment or url.path.rstrip('/') != '/v1'):
        raise ValueError('DIRECT_LOOPBACK_API_REQUIRED')
    root = base.rstrip('/')[:-3]
    def read(path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        with request(Request(root + path, data=body, headers={'Content-Type': 'application/json'}), timeout=180) as reply:
            return json.load(reply)
    health = read('/health')
    if health.get('status') != 'ready' or health.get('workers') != 8:
        raise ValueError('EIGHT_WORKERS_NOT_READY')
    models = read('/v1/models')['data']
    model = next(m for m in models if m['id'] == 'ds-v4.1-flash-1017')
    if (model.get('max_model_len') != 1048576 or model.get('max_output_tokens') != 32768
            or model.get('max_images_per_request') != 999 or model.get('dspark') is not False):
        raise ValueError('SERVING_PROFILE_MISMATCH')
    response = read('/v1/chat/completions', {
        'model': model['id'], 'messages': [{'role': 'user', 'content': 'Reply with the word ready.'}],
        'max_tokens': 32, 'temperature': 0, 'stream': False,
        'thinking': {'type': 'disabled'}})
    if not response['choices'][0]['message'].get('content'):
        raise ValueError('EMPTY_MODEL_REPLY')
    return {'state': 'SHORT_JSON_SMOKE_PASS_NOT_FULL_ACCEPTANCE'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8900/v1')
    print(json.dumps(check(parser.parse_args().base)))
