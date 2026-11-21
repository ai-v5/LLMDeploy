from copy import deepcopy
import unittest

from deploy_model_router.caddy import prepare_servers, proxies


def proxy():
    return {'handler': 'reverse_proxy', 'upstreams': [{'dial': '127.0.0.1:20001'},
                                                       {'dial': '127.0.0.1:20002'}],
            'health_checks': {'passive': {'unhealthy_status': [503]}},
            'headers': {'response': {'set': {'X-LLMDeploy-Service': ['engine'],
                                              'X-LLMDeploy-Gateway': ['caddy']}}}}


def site(port):
    return {'listen': [f'0.0.0.0:{port}'], 'routes': [
        {'match': [{'header': {'Incremental': ['*']}}], 'handle': [{'handler': 'subroute',
         'routes': [{'handle': [proxy()]}]}]}, {'handle': [proxy()]}]}


class CaddyTransformationTests(unittest.TestCase):
    def test_existing_routes_remain_with_isolated_model_errors(self):
        source = {'front': site(19000), 'fallback': site(19001)}
        before = deepcopy(source)
        result = prepare_servers(source, front_port=19000, fallback_port=19001, router_port=19002)
        self.assertEqual(source, before)
        front, fallback = list(proxies(result['front'])), list(proxies(result['fallback']))
        self.assertEqual(len(front), 2)
        for p in front:
            self.assertEqual(p['upstreams'], [{'dial': '127.0.0.1:19002'}, {'dial': '127.0.0.1:19001'}])
            self.assertNotIn('unhealthy_status', p['health_checks']['passive'])
            self.assertNotIn('retry_match', p['load_balancing'])
            self.assertNotIn('X-LLMDeploy-Service', p['headers']['response']['set'])
        for p in fallback:
            self.assertEqual(p['upstreams'], proxy()['upstreams'])
            self.assertEqual(p['headers']['request']['set']['X-Forwarded-For'], ['{http.vars.client_ip}'])
        self.assertEqual(result['fallback']['trusted_proxies']['ranges'], ['127.0.0.1/32', '::1/128'])

    def test_rejects_mismatched_routes_and_unknown_trust(self):
        source = {'front': site(19000), 'fallback': site(19001)}
        source['fallback']['routes'][1]['handle'][0]['upstreams'][0]['dial'] = '127.0.0.1:29999'
        with self.assertRaises(ValueError):
            prepare_servers(source, front_port=19000, fallback_port=19001, router_port=19002)
        source = {'front': site(19000), 'fallback': site(19001)}
        source['fallback']['trusted_proxies'] = {'source': 'static', 'ranges': ['10.0.0.0/8']}
        with self.assertRaises(ValueError):
            prepare_servers(source, front_port=19000, fallback_port=19001, router_port=19002)


if __name__ == '__main__':
    unittest.main()
