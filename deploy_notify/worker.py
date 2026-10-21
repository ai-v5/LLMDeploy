"""Independent local observer. Never changes model, gateway or container state."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback
from urllib.parse import urlsplit

from .backends import build_backend, Unavailable, Rejected, Uncertain
from .incoming import WelinkHistory, consume
from .tracker import Tracker, event_content, status_text
from .usage import fetch_all as fetch_usage, validate as validate_usage


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    allowed = {'backend', 'backend_state', 'nodes', 'down_seconds', 'node_down_seconds', 'recovery_seconds',
               'freshness_seconds', 'poll_seconds', 'incoming', 'usage', 'engine_name'}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError('INVALID_CONFIG_FIELDS')
    if config.get('backend') is None:
        return config
    backend = config['backend']
    if set(backend) != {'type', 'endpoint', 'receiver', 'token_file'}:
        raise ValueError('BACKEND_FIELDS_REQUIRE_TOKEN_FILE_ONLY')
    build_backend(backend)
    url = urlsplit(backend['endpoint'])
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.query or url.fragment or not isinstance(backend['receiver'], str) or not backend['receiver']):
        raise ValueError('INVALID_BACKEND')
    url.port
    nodes = config.get('nodes')
    if not isinstance(nodes, dict) or len(nodes) < 2:
        raise ValueError('MULTIPLE_BACKENDS_REQUIRED')
    if config.get('engine_name', 'vLLM') not in ('vLLM', 'DeepInfer'):
        raise ValueError('INVALID_ENGINE_NAME')
    if any(not p.isdecimal() or not 1 <= int(p) <= 65535 or not isinstance(label, str)
           or not label for p, label in nodes.items()):
        raise ValueError('INVALID_NODES')
    defaults = {'down_seconds': 60, 'node_down_seconds': 86400, 'recovery_seconds': 10,
                'freshness_seconds': 15, 'poll_seconds': 2}
    for key, default in defaults.items():
        value = config.setdefault(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('INVALID_TIMING')
    if config['poll_seconds'] >= config['freshness_seconds']:
        raise ValueError('POLL_MUST_BE_FASTER_THAN_FRESHNESS')
    if config.get('usage') is not None:
        validate_usage(config['usage'], nodes)
    for obj, key in [(config, 'backend_state'), (backend, 'token_file')]:
        value = Path(obj[key])
        obj[key] = str((value if value.is_absolute() else path.parent / value).resolve())
    incoming = config.get('incoming')
    if incoming is not None:
        if (set(incoming) != {'type', 'executable', 'bot_name', 'bot_uid'}
                or incoming['type'] != 'welink-cli'
                or not all(isinstance(incoming[k], str) and incoming[k]
                           for k in ('executable', 'bot_name', 'bot_uid'))):
            raise ValueError('INVALID_INCOMING')
        executable = Path(incoming['executable'])
        incoming['executable'] = str((executable if executable.is_absolute() else path.parent / executable).resolve())
    return config


def write_json(path, value):
    tmp = path.with_suffix('.new')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def read_snapshot(config):
    try:
        return json.loads(Path(config['backend_state']).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, KeyError):
        return {}


def dispatch(tracker, save, backend, now):
    for item in tracker.state['outbox']:
        if item['status'] != 'pending' or item['next_try'] > now:
            continue
        if item['kind'] == 'STATUS_REPLY':
            if now >= item.get('expires_epoch', item['epoch'] + 120):
                item.update(status='expired', completed_epoch=now)
                save()
                continue
            item['content'] = event_content(status_text(tracker.config, read_snapshot(tracker.config), now), item['epoch'])
        elif (item['kind'] in ('NODE_DOWN', 'BOTH_DOWN', 'NODE_RECOVERED', 'SERVICE_RECOVERED')
              and now - item['epoch'] > tracker.config['freshness_seconds'] and not item.get('delayed')):
            item['content'] = '【历史事件补发，以下为事件发生时的状态】\n' + item['content']
            item['delayed'] = True
        try:
            backend.token()
        except Unavailable:
            tracker.state['delivery'] = 'WAITING_CREDENTIALS'
            save()
            return
        item.update(status='sending', attempts=item['attempts'] + 1)
        save()
        try:
            item['result'] = backend.send(item['content'])
            item.update(status='accepted', completed_epoch=now)
            tracker.state['delivery'] = 'SERVICE_ACCEPTED_NOT_DELIVERY_RECEIPT'
        except Uncertain as error:
            item.update(status='uncertain', result=str(error))
            tracker.state['delivery'] = 'UNCERTAIN_NO_AUTOMATIC_RESEND'
        except (Unavailable, Rejected) as error:
            item.update(status='pending', result=str(error),
                        next_try=now + min(300, 10 * 2 ** min(item['attempts'] - 1, 5)))
            tracker.state['delivery'] = str(error)
        save()


def recover_sending(state):
    for item in state['outbox']:
        if item['status'] == 'sending':
            item.update(status='uncertain', result='INTERRUPTED_DELIVERY_NO_RESEND')


def singleton(root, role='worker'):
    if os.name != 'nt':
        raise ValueError('WINDOWS_WORKER_REQUIRED')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    name = 'Local\\LLMDeployNotify_' + role + '_' + hashlib.sha256(str(root).lower().encode()).hexdigest()[:20]
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise OSError('MUTEX_CREATE_FAILED')
    return None if ctypes.get_last_error() == 183 else handle


def run(config, root, once=False, send_test=False):
    backend = build_backend(config.get('backend'))
    if backend is None:
        return 'DISABLED'
    lock = singleton(root)
    if lock is None:
        return 'ALREADY_RUNNING'
    path = root / 'alert-state.json'
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    tracker = Tracker(config, state)
    recover_sending(tracker.state)
    save = lambda: write_json(path, tracker.state)
    if send_test:
        snapshot = read_snapshot(config)
        now = time.time()
        tracker.enqueue('ACTIVATION_TEST', '【部署通知接入测试，不代表真实故障】\n' +
                        status_text(config, snapshot, now), now)
        save()
        dispatch(tracker, save, backend, now)
        return tracker.state.get('delivery', 'NO_DELIVERY')
    incoming = config.get('incoming')
    reader = WelinkHistory(incoming, config['backend']['receiver']) if incoming else None
    usage_settings = config.get('usage')
    pool = ThreadPoolExecutor(max_workers=2) if reader or usage_settings else None
    future, next_read = None, 0
    usage_future, next_usage = None, 0
    try:
        while True:
            now = time.time()
            snapshot = read_snapshot(config)
            tracker.observe(snapshot, now)
            if usage_future is not None and usage_future.done():
                try:
                    config['_usage_snapshot'] = usage_future.result()
                except Exception:
                    config['_usage_snapshot'] = {}
                tracker.state['usage'] = config['_usage_snapshot']
                usage_future = None
            if usage_settings and usage_future is None and now >= next_usage:
                usage_future = pool.submit(fetch_usage, usage_settings)
                next_usage = now + 30
            tracker.state.update(worker_pid=os.getpid(), worker_epoch=now)
            try:
                backend.token()
                tracker.state['credential_status'] = 'PRESENT_VALIDITY_UNVERIFIED'
            except Unavailable:
                tracker.state['credential_status'] = 'WAITING_CREDENTIALS'
            if future is not None and future.done():
                try:
                    consume(tracker, future.result(), incoming, snapshot, now)
                    tracker.state.pop('incoming_error', None)
                except Exception as error:
                    tracker.state['incoming'] = 'HISTORY_UNAVAILABLE'
                    tracker.state['incoming_error'] = {
                        'type': type(error).__name__,
                        'winerror': getattr(error, 'winerror', None),
                        'executable_exists': Path(incoming['executable']).exists(),
                        'frames': [{'function': f.name, 'line': f.lineno}
                                   for f in traceback.extract_tb(error.__traceback__)],
                    }
                future = None
            if reader and future is None and now >= next_read:
                future = pool.submit(reader.fetch)
                next_read = now + 10
            tracker.state['outbox'] = [x for x in tracker.state['outbox']
                if x['status'] not in ('accepted', 'delivered', 'expired') or now - x.get('completed_epoch', now) < 30 * 86400]
            save()
            dispatch(tracker, save, backend, now)
            if once:
                return 'OBSERVED'
            time.sleep(config['poll_seconds'])
    finally:
        if pool:
            pool.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Validate configuration without network or writes')
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--send-test', action='store_true', help='Explicitly send a labelled activation test')
    mode.add_argument('--export-config', metavar='NEW_FILE', help='Write validated absolute paths for an independent runtime')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.export_config:
            with Path(args.export_config).open('x', encoding='utf-8') as output:
                json.dump(config, output, ensure_ascii=False, indent=2)
            print('RUNTIME_CONFIG_EXPORTED')
            return 0
        if args.check:
            print('CONFIG_VALID' if config.get('backend') else 'DISABLED')
            return 0
        result = run(config, Path(args.config).resolve().parent, args.once, args.send_test)
        print(result)
        if args.send_test and result != 'SERVICE_ACCEPTED_NOT_DELIVERY_RECEIPT':
            return 1
        return 0
    except Exception as error:
        # Configuration and transport exception values can contain private data.
        print('NOTIFIER_FAILED:' + type(error).__name__, file=sys.stderr)
        return 1
