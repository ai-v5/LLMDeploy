"""Authenticated single-pair P/D lab proxy with mandatory remote-prefill metadata."""
import argparse
from contextlib import asynccontextmanager
import copy
import hmac
import json
import os
import time
import uuid


def prefill_request(body):
    result = copy.deepcopy(body)
    result['stream'] = False
    result.pop('stream_options', None)
    result['max_tokens'] = result['min_tokens'] = 1
    if 'max_completion_tokens' in result:
        result['max_completion_tokens'] = 1
    result['kv_transfer_params'] = {
        'do_remote_decode': True, 'do_remote_prefill': False,
        'remote_engine_id': None, 'remote_block_ids': None,
        'remote_host': None, 'remote_port': None,
    }
    return result


def decode_request(body, prefill_response):
    transfer = prefill_response.get('kv_transfer_params') or {}
    required = ('remote_engine_id', 'remote_block_ids', 'remote_request_id', 'remote_host', 'remote_port')
    if not transfer.get('do_remote_prefill') or not all(transfer.get(k) for k in required):
        raise ValueError('PREFILL_DID_NOT_RETURN_REMOTE_KV')
    result = copy.deepcopy(body)
    result['kv_transfer_params'] = transfer
    return result


def create_app(prefill_url, decode_url, api_key):
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    if not api_key or len(api_key) < 24:
        raise ValueError('INDEPENDENT_API_KEY_REQUIRED')

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(36000, connect=15), trust_env=False) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan)

    @app.middleware('http')
    async def authenticate(request, call_next):
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + api_key):
            return JSONResponse({'error': {'message': 'Unauthorized'}}, status_code=401)
        return await call_next(request)

    @app.get('/health')
    async def health():
        for base in (prefill_url, decode_url):
            try:
                response = await app.state.client.get(base + '/health', headers={'Authorization': 'Bearer ' + api_key})
                if response.status_code != 200:
                    return JSONResponse({'status': 'upstream_unhealthy'}, status_code=503)
            except httpx.HTTPError:
                return JSONResponse({'status': 'upstream_unreachable'}, status_code=503)
        return {'status': 'healthy'}

    @app.get('/v1/models')
    async def models():
        response = await app.state.client.get(decode_url + '/v1/models', headers={'Authorization': 'Bearer ' + api_key})
        return Response(response.content, status_code=response.status_code, media_type='application/json')

    @app.post('/v1/{endpoint:path}')
    async def complete(endpoint: str, request: Request):
        if endpoint not in ('completions', 'chat/completions'):
            return JSONResponse({'error': {'message': 'Unsupported endpoint'}}, status_code=404)
        body = await request.json()
        request_id = 'pd-' + uuid.uuid4().hex
        headers = {'Authorization': 'Bearer ' + api_key, 'X-Request-Id': request_id}
        client = app.state.client
        prefill_start = time.perf_counter()
        try:
            p = await client.post(prefill_url + '/v1/' + endpoint, json=prefill_request(body), headers=headers)
            if p.status_code != 200:
                return JSONResponse({'error': {'message': 'Prefill failed', 'status': p.status_code}}, status_code=502)
            d_body = decode_request(body, p.json())
            prefill_seconds = time.perf_counter() - prefill_start
            usage = p.json().get('usage') or {}
            prompt_details = usage.get('prompt_tokens_details') or {}
            # Open D before returning HTTP 200; upstream failure must remain a failure.
            d = await client.send(client.build_request('POST', decode_url + '/v1/' + endpoint,
                                                       json=d_body, headers=headers), stream=True)
            if d.status_code != 200:
                await d.aclose()
                return JSONResponse({'error': {'message': 'Decode failed', 'status': d.status_code}}, status_code=502)
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            return JSONResponse({'error': {'message': 'P/D transfer request failed'}}, status_code=502)

        async def chunks():
            try:
                async for chunk in d.aiter_bytes():
                    yield chunk
            finally:
                await d.aclose()
        media = 'text/event-stream' if body.get('stream') else 'application/json'
        receipt_headers = {
            'X-Request-Id': request_id,
            'X-PD-Prefill-Seconds': f'{prefill_seconds:.6f}',
            'X-PD-Prefill-Prompt-Tokens': str(int(usage.get('prompt_tokens') or 0)),
            'X-PD-Prefill-Cached-Tokens': str(int(prompt_details.get('cached_tokens') or 0)),
            'X-PD-Prefill-Request-Id': str(d_body['kv_transfer_params']['remote_request_id']),
        }
        return StreamingResponse(chunks(), media_type=media, headers=receipt_headers)

    return app


if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefill', required=True)
    parser.add_argument('--decode', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=28412)
    args = parser.parse_args()
    app = create_app(args.prefill.rstrip('/'), args.decode.rstrip('/'), os.environ['VLLM_API_KEY'])
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
