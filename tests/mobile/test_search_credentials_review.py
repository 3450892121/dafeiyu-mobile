"""Exercise both search clients with fictional credentials and intercepted HTTP."""
import io
import json
import unittest
from unittest.mock import patch
import urllib.request

import websearch


class Config(dict):
    def models(self):
        return self.get('models', [])


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode('utf-8'))


class SearchCredentialReviewTests(unittest.TestCase):
    def test_real_ark_and_deepseek_client_paths_keep_keys_only_on_original_request(self):
        for provider, base, endpoint, payload in (
            ('ark', 'https://ark.cn-beijing.volces.com/api/plan/v3',
             websearch.URL, {'Result': {'WebResults': []}}),
            ('deepseek', 'https://api.deepseek.com/v1',
             websearch.DS_URL, {'results': []}),
        ):
            with self.subTest(provider=provider):
                key = 'fictional-' + provider + '-search-only'
                cfg = Config(models=[{'base_url': base, 'api_key': key, 'enabled': True}])
                captured = []

                def transport(request, **kwargs):
                    captured.append(request)
                    return Response(payload)

                with patch.object(websearch.urllib.request, 'urlopen', side_effect=transport):
                    self.assertEqual(websearch.web_search(cfg, '虚构搜索问题', provider=provider), [])
                self.assertEqual(len(captured), 1)
                original = captured[0]
                self.assertEqual(original.full_url, endpoint)
                self.assertEqual(original.get_header('Authorization'), 'Bearer ' + key)
                for destination in ('https://unrelated.example.invalid/search', endpoint + '/redirected'):
                    redirected = urllib.request.HTTPRedirectHandler().redirect_request(
                        original, None, 302, 'moved', {}, destination)
                    self.assertIsNone(redirected.get_header('Authorization'))

    def test_mismatched_disabled_and_nonplan_model_routes_do_not_supply_search_credentials(self):
        cfg = Config(models=[
            {'base_url': 'https://proxy.example.invalid/v1', 'api_key': 'fictional-proxy'},
            {'base_url': 'https://ark.cn-beijing.volces.com/api/v3', 'api_key': 'fictional-regular-ark'},
            {'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
             'api_key': 'fictional-disabled-ark', 'enabled': False},
            {'base_url': 'https://api.deepseek.com/v1',
             'api_key': 'fictional-disabled-deepseek', 'enabled': False},
        ])
        with patch.object(websearch.urllib.request, 'urlopen') as transport:
            for provider in ('ark', 'deepseek'):
                with self.subTest(provider=provider), self.assertRaises(websearch.SearchUnavailable):
                    websearch.web_search(cfg, '虚构搜索问题', provider=provider)
            transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
