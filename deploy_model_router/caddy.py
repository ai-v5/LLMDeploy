"""Prepare an atomic Caddy servers update without altering other routes."""
from copy import deepcopy


def proxies(value):
    if isinstance(value, dict):
        if value.get('handler') == 'reverse_proxy':
            yield value
        for child in value.values():
            yield from proxies(child)
    elif isinstance(value, list):
        for child in value:
            yield from proxies(child)


def prepare_servers(servers, *, front_port, fallback_port, router_port):
    """Return a copy; reject a topology whose safety cannot be established."""
    if not all(type(x) is int and 1 <= x <= 65535 for x in (front_port, fallback_port, router_port)):
        raise ValueError('Invalid port')
    if len({front_port, fallback_port, router_port}) != 3 or not isinstance(servers, dict):
        raise ValueError('Ports must be distinct')
    result = deepcopy(servers)

    def find(port):
        found = [s for s in result.values() if isinstance(s, dict)
                 and any(isinstance(x, str) and x.rsplit(':', 1)[-1] == str(port)
                         for x in s.get('listen', []))]
        if len(found) != 1 or len(found[0].get('listen', [])) != 1:
            raise ValueError('Expected one dedicated server per port')
        return found[0]

    front, fallback = find(front_port), find(fallback_port)
    if front is fallback or fallback.get('trusted_proxies'):
        raise ValueError('Unsupported trusted-proxy topology')
    front_proxies = list(proxies(front.get('routes', [])))
    fallback_proxies = list(proxies(fallback.get('routes', [])))
    if not front_proxies or len(front_proxies) != len(fallback_proxies):
        raise ValueError('Unexpected proxy routes')
    old = [p.get('upstreams') for p in fallback_proxies]
    if any(not isinstance(x, list) or not x for x in old):
        raise ValueError('No fallback upstreams')
    if [p.get('upstreams') for p in front_proxies] != old:
        raise ValueError('Frontend and fallback routes differ')
    forbidden = {f'127.0.0.1:{p}' for p in (front_port, fallback_port, router_port)}
    for items in old:
        if any(not isinstance(x, dict) or not isinstance(x.get('dial'), str)
               or x['dial'] in forbidden for x in items):
            raise ValueError('Unexpected fallback upstream')
    for proxy in front_proxies:
        proxy['upstreams'] = [{'dial': f'127.0.0.1:{router_port}'},
                              {'dial': f'127.0.0.1:{fallback_port}'}]
        proxy['load_balancing'] = {'selection_policy': {'policy': 'first'},
                                   'try_duration': 5_000_000_000, 'try_interval': 200_000_000}
        # A model-specific HTTP error must not take down the shared frontend.
        proxy['health_checks'] = {'passive': {'fail_duration': 5_000_000_000, 'max_fails': 1}}
        response_set = proxy.get('headers', {}).get('response', {}).get('set', {})
        for name in tuple(response_set):
            if name.lower() == 'x-llmdeploy-service':
                del response_set[name]
    fallback['trusted_proxies'] = {'source': 'static', 'ranges': ['127.0.0.1/32', '::1/128']}
    for proxy in fallback_proxies:
        request_set = proxy.setdefault('headers', {}).setdefault('request', {}).setdefault('set', {})
        if any(k.lower() == 'x-forwarded-for' for k in request_set):
            raise ValueError('Existing client-IP override needs review')
        request_set['X-Forwarded-For'] = ['{http.vars.client_ip}']
    return result
