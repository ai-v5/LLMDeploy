"""Measure first-token and per-token latency for a local OpenAI-compatible endpoint.

Design notes:
* TTFT is measured to the first streamed delta of any kind (thinking or content),
  because that is when the engine actually starts emitting. A second, stricter
  clock records the first *content* delta, which is what an end user perceives.
* TPOT divides the post-first-token window by the server's own
  `completion_tokens - 1`, so prefill work never leaks into the decode rate.
* Receipts record the scheme, port and label only. Host addresses stay out of the
  file so results can be published without leaking machine identity.
* A case may declare `turns` (several request rounds over the same endpoint) and
  a `haystack` block (generated long context). Both are built from in-repo text,
  so every case is reproducible offline and carries no customer data.

Usage:
    python tools/bench/bench.py --base-url http://127.0.0.1:8900/v1 \
        --model <served-name> --engine-label <label> \
        --config config.json --out record/result.json
"""
import argparse
import base64
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Neutral filler for generated long context: original prose with no client data.
FILLER = (
    '上下文填充段落。推理服务的输入长度会显著影响首 token 时间，因此在同一套用例里'
    '必须固定填充文本与插入位置，才能让不同引擎的结果可比。本段仅用于占位，'
    '不包含任何真实业务数据。The filler paragraph is intentionally repetitive so the '
    'prompt length is predictable across engine builds. '
)


def load_cases(entries):
    """Load every case JSON under a directory, or the given files in order."""
    found = []
    for entry in entries:
        target = Path(entry)
        found.extend(sorted(target.glob('*.json')) if target.is_dir() else [target])
    cases = [json.loads(path.read_text(encoding='utf-8')) for path in found]
    if not cases:
        raise SystemExit('NO_CASES')
    return cases


def read_asset(name):
    return base64.b64encode((HERE / 'assets' / name).read_bytes()).decode()


def read_snippet(name):
    return (HERE / 'cases' / 'snippets' / name).read_text(encoding='utf-8')


def build_haystack(spec):
    """Place a needle in the middle of generated filler of roughly the asked size."""
    haystack_chars = max(1, int(spec['tokens'] * 1.6))
    repeats = max(1, haystack_chars // len(FILLER))
    body = FILLER * repeats
    needle = spec['needle']
    middle = len(body) // 2
    return body[:middle] + '\n' + needle + '\n' + body[middle:]


def render_content(content, haystack):
    if isinstance(content, str):
        text = content
        if '{snippet}' in text:
            text = text.replace('{snippet}', read_snippet(haystack['snippet'])) if haystack.get('snippet') else text
        if '{haystack}' in text:
            text = text.replace('{haystack}', haystack.get('body', ''))
        return text
    parts = []
    for part in content:
        if part.get('type') == 'image':
            parts.append({'type': 'image_url',
                          'image_url': {'url': 'data:image/png;base64,'
                                               + read_asset(part['asset'])}})
        else:
            parts.append(dict(part))
    return parts


def build_payload(case, model):
    """Turn one request round of a case into an API payload."""
    haystack = {}
    if case.get('haystack'):
        haystack['body'] = build_haystack(case['haystack'])
    if case.get('snippet'):
        haystack['snippet'] = case['snippet']
    messages = [{'role': message['role'],
                 'content': render_content(message['content'], haystack)}
                for message in case['messages']]
    payload = {'model': model, 'messages': messages, 'stream': True,
               'stream_options': {'include_usage': True},
               'max_tokens': case.get('max_tokens', 1024),
               'temperature': case.get('temperature', 0)}
    if case.get('tools'):
        payload['tools'] = case['tools']
        payload['tool_choice'] = case.get('tool_choice', 'auto')
    if case.get('response_format'):
        payload['response_format'] = case['response_format']
    return payload


def stream_once(base, payload, timeout=900, api_key=None):
    # The key travels in a header only: never in argv, receipts or logs.
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    request = urllib.request.Request(
        base.rstrip('/') + '/chat/completions',
        data=json.dumps(payload).encode(), headers=headers)
    started = time.monotonic()
    first_any = first_content = None
    usage = None
    content_parts = []
    tool_calls = 0
    with OPENER.open(request, timeout=timeout) as response:
        for raw in response:
            line = raw.strip()
            if not line.startswith(b'data: '):
                continue
            body = line[6:]
            if body == b'[DONE]':
                break
            try:
                event = json.loads(body)
            except json.JSONDecodeError:
                continue
            if event.get('usage'):
                usage = event['usage']
            for choice in event.get('choices') or []:
                delta = choice.get('delta') or {}
                # This vLLM build names thinking deltas `reasoning`; accept the
                # OpenAI spelling too so the probe survives a rename.
                thinking = delta.get('reasoning') or delta.get('reasoning_content')
                text = delta.get('content')
                if (thinking or text) and first_any is None:
                    first_any = time.monotonic() - started
                if text:
                    if first_content is None:
                        first_content = time.monotonic() - started
                    content_parts.append(text)
                tool_calls += len(delta.get('tool_calls') or [])
    total = time.monotonic() - started
    return {'first_any_s': round(first_any, 4) if first_any else None,
            'first_content_s': round(first_content, 4) if first_content else None,
            'total_s': round(total, 3), 'usage': usage,
            'chars': len(''.join(content_parts)), 'tool_call_deltas': tool_calls}


def median_or_none(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {'median': round(statistics.median(values), 3),
            'min': round(min(values), 3), 'max': round(max(values), 3)}


def summarize_runs(runs_all):
    runs = runs_all
    rows = []
    for run in runs:
        usage = run.get('usage') or {}
        completion = usage.get('completion_tokens') or 0
        decode_window = (run['total_s'] - run['first_any_s']) if run['first_any_s'] else 0
        rows.append({'prompt_tokens': usage.get('prompt_tokens'),
                     'completion_tokens': completion,
                     'reasoning_tokens': (usage.get('completion_tokens_details') or {})
                     .get('reasoning_tokens'),
                     'ttft_s': run['first_any_s'],
                     'ttft_content_s': run['first_content_s'],
                     'total_s': run['total_s'],
                     'content_chars': run.get('chars'),
                     'tool_call_deltas': run.get('tool_call_deltas'),
                     'tpot_ms': round(1000 * decode_window / (completion - 1), 2)
                     if completion > 1 and decode_window > 0 else None,
                     'e2e_tok_s': round(completion / run['total_s'], 2) if completion else None})
    ok = [r for r in rows if r['ttft_s'] is not None]
    errors = [r['error'] for r in runs_all if isinstance(r, dict) and r.get('error')]
    return {'samples': len(ok), 'failures': len(rows) - len(ok), 'errors': errors[:3],
            'prompt_tokens': rows[0]['prompt_tokens'] if rows else None,
            'completion_tokens': rows[0]['completion_tokens'] if rows else None,
            'reasoning_tokens': rows[0]['reasoning_tokens'] if rows else None,
            'ttft_s': median_or_none(r['ttft_s'] for r in rows),
            'ttft_content_s': median_or_none(r['ttft_content_s'] for r in rows),
            'tpot_ms': median_or_none(r['tpot_ms'] for r in rows),
            'e2e_tok_s': median_or_none(r['e2e_tok_s'] for r in rows),
            'runs': rows}


def run_burst(base, case, model, concurrency, api_key=None):
    gate = threading.Barrier(concurrency)
    results = [None] * concurrency

    def worker(index):
        try:
            gate.wait()
            results[index] = stream_once(base, build_payload(case, model), api_key=api_key)
        except Exception as exc:  # noqa: BLE001 - keep the burst meaningful
            results[index] = {'error': type(exc).__name__ + ': ' + str(exc)[:160]}

    boots = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    started = time.monotonic()
    for boot in boots:
        boot.start()
    for boot in boots:
        boot.join()
    span = time.monotonic() - started
    tokens = sum(r['usage']['completion_tokens'] for r in results
                 if r and r.get('usage'))
    return {'concurrency': concurrency, 'wall_s': round(span, 2),
            'completion_tokens_total': tokens,
            'aggregate_tok_s': round(tokens / span, 2) if tokens else None,
            'errors': [r['error'] for r in results if r and 'error' in r]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--cases', default=str(HERE / 'cases'))
    parser.add_argument('--engine-label', required=True)
    parser.add_argument('--config', help='JSON file describing the engine configuration')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--concurrency', type=int, default=0)
    parser.add_argument('--only', nargs='*', help='Restrict to these case ids')
    parser.add_argument('--out', required=True)
    parser.add_argument('--api-key-env', help='Environment variable holding the bearer token')
    args = parser.parse_args()

    base = args.base_url
    parsed = urllib.parse.urlparse(base)
    cases = load_cases([args.cases])
    if args.only:
        cases = [c for c in cases if c['id'] in args.only]
        if not cases:
            raise SystemExit('NO_CASES_AFTER_FILTER')
    config = json.loads(Path(args.config).read_text(encoding='utf-8')) if args.config else {}
    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    probe_headers = {'Authorization': 'Bearer ' + api_key} if api_key else {}

    with OPENER.open(urllib.request.Request(base.rstrip('/') + '/models',
                                           headers=probe_headers), timeout=60) as response:
        served = json.loads(response.read())

    receipt = {
        'schema': 2,
        'endpoint': {'scheme': parsed.scheme,
                     'host_scope': 'loopback' if parsed.hostname in
                     ('127.0.0.1', 'localhost', '::1') else 'private-network',
                     'port': parsed.port, 'path': parsed.path},
        'engine': {'label': args.engine_label, 'config': config},
        'served_models': served,
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'repeats': args.repeats,
        'cases': {},
        'bursts': {},
    }

    for case in cases:
        rounds = case.get('turns') or [case]
        per_turn = []
        for index in range(len(rounds)):
            turn_case = dict(case, **rounds[index])
            runs = []
            for _ in range(args.repeats):
                try:
                    runs.append(stream_once(base, build_payload(turn_case, args.model),
                                            api_key=api_key))
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode('utf-8', 'replace')[:300]
                    runs.append({'first_any_s': None, 'first_content_s': None,
                                 'total_s': 0.0, 'usage': None,
                                 'error': f'HTTP {exc.code}: {body}'})
            summary = summarize_runs(runs)
            if len(rounds) > 1:
                summary['turn'] = index + 1
            per_turn.append(summary)
        entry = per_turn[0] if len(per_turn) == 1 else {'turns': per_turn}
        entry['title'] = case.get('title')
        entry['kind'] = case.get('kind')
        receipt['cases'][case['id']] = entry
        if args.concurrency:
            burst = run_burst(base, case, args.model, args.concurrency, api_key=api_key)
            burst['case'] = case['id']
            receipt['bursts'][case['id']] = burst
        shown = (entry if 'turns' not in entry
                 else {'turns': [turn['ttft_s'] for turn in entry['turns']]})
        print(f"[{case['id']}] " + json.dumps(
            {key: shown[key] for key in ('ttft_s', 'tpot_ms', 'turns') if key in shown},
            default=str), file=sys.stderr, flush=True)

    receipt['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'out': str(out), 'cases': list(receipt['cases'])}, ensure_ascii=False))


if __name__ == '__main__':
    sys.exit(main())

