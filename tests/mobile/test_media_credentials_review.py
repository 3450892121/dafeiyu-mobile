"""Media and model credentials use fake keys/transports and never contact real services."""
import io
import json
import unittest
from unittest.mock import Mock, patch
import urllib.request

import flow_media
from flow_media import MediaMixin
from model import Ark, ModelPool
import model
import sticker_index


BASE = 'https://ark.cn-beijing.volces.com/api/plan/v3'
FAKE_KEY = 'fictional-ark-only-key'


class Config(dict):
    def models(self):
        return self.get('models', [])


def route(base=BASE, key=FAKE_KEY, **values):
    return {'id': 'fixture', 'model': 'fictional-model', 'base_url': base,
            'api_key': key, 'enabled': True, **values}


class Response(io.BytesIO):
    status = 200

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())


class MediaCredentialTests(unittest.TestCase):
    def cfg(self, *routes):
        return Config(models=list(routes), api_key='fictional-unbound-global-key')

    def test_drawing_and_embeddings_never_borrow_an_unrelated_or_global_key(self):
        for cfg in (self.cfg(), self.cfg(route('https://other.invalid/v1'))):
            with self.subTest(cfg=bool(cfg['models'])):
                bot = MediaMixin()
                bot.cfg = cfg
                self.assertEqual(bot._draw_key(), '')
                self.assertEqual(sticker_index._cfg_key(cfg), '')

    def test_matching_enabled_base_is_selected_after_unrelated_and_disabled_routes(self):
        cfg = self.cfg(route('https://other.invalid/v1'), route(enabled=False),
                       route(key=''), route(BASE + '/'))
        bot = MediaMixin()
        bot.cfg = cfg
        self.assertEqual(bot._draw_key(), FAKE_KEY)
        self.assertEqual(sticker_index._cfg_key(cfg), FAKE_KEY)

    def test_same_host_different_plans_or_paths_do_not_share_credentials(self):
        for path in ('/api/v3', '/api/coding/v3', '/api/plan/v3-extra',
                     '/api/plan/v3/images/generations', ''):
            with self.subTest(path=path):
                cfg = self.cfg(route('https://ark.cn-beijing.volces.com' + path))
                self.assertEqual(sticker_index._cfg_key(cfg), '')
                bot = MediaMixin()
                bot.cfg = cfg
                self.assertEqual(bot._draw_key(), '')

    def test_origin_match_rejects_spoofs_plain_http_userinfo_and_nondefault_ports(self):
        values = (
            'https://ark.cn-beijing.volces.com.evil.invalid/api/plan/v3',
            'https://evil.invalid/ark.cn-beijing.volces.com/api/plan/v3',
            'http://ark.cn-beijing.volces.com/api/plan/v3',
            'https://user@ark.cn-beijing.volces.com/api/plan/v3',
            BASE + '?route=x', BASE + '#route',
            'https://ark.cn-beijing.volces.com:444/api/plan/v3',
            'https://ark.cn-beijing.volces.com:0/api/plan/v3',
            'https://ark.cn-beijing.volces.com:bad/api/plan/v3',
            'https://ark.cn-beijing.volces.com/\napi/plan/v3',
        )
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(sticker_index._cfg_key(self.cfg(route(value))), '')
        self.assertEqual(sticker_index._cfg_key(self.cfg(route(
            'https://ARK.CN-BEIJING.VOLCES.COM:443/api/plan/v3'))), FAKE_KEY)

    def test_malformed_routes_do_not_hide_a_valid_following_route(self):
        self.assertEqual(sticker_index._cfg_key(self.cfg(None, [], 'bad', route())), FAKE_KEY)

    def test_model_configuration_changes_are_used_without_an_old_key_cache(self):
        cfg = self.cfg(route())
        self.assertEqual(sticker_index._cfg_key(cfg), FAKE_KEY)
        cfg['models'] = [route('https://other.invalid/v1', key='fictional-other-key')]
        self.assertEqual(sticker_index._cfg_key(cfg), '')

    def test_no_matching_media_service_returns_clear_error_before_any_request(self):
        cfg = self.cfg(route('https://other.invalid/v1'))
        bot = MediaMixin()
        bot.cfg, bot.store = cfg, object()
        bot._send = Mock()
        bot._tool_owner_remind = Mock()
        bot._draw_slot_end = Mock()
        with patch.object(flow_media.tools_gate, 'note'), patch.object(urllib.request, 'urlopen') as transport:
            bot._draw('fixture', 'p:101', False, 0, 101)
            self.assertEqual(sticker_index.embed_texts(cfg, ['fixture']), [])
            for build in (sticker_index.build_cards_index, sticker_index.build_text_index):
                result = build('unread-fixture-path', cfg)
                self.assertFalse(result['ok'])
                self.assertIn('/api/plan/v3', result['error'])
            transport.assert_not_called()
        notice = bot._send.call_args.args[-1][0]['data']['text']
        self.assertIn('AI 接口设置', notice)
        self.assertIn('/api/plan/v3', notice)

    def test_drawing_request_carries_only_the_matching_route_key_and_drops_it_on_redirect(self):
        bot = MediaMixin()
        bot.cfg, bot.store = self.cfg(route('https://other.invalid/v1'), route()), object()
        bot._send = Mock()
        bot._tool_owner_remind = Mock()
        bot._draw_prompt_text = Mock(return_value='一棵树和一只鸟')
        bot._draw_slot_end = Mock()
        captured = []

        def transport(request, **kwargs):
            captured.append(request)
            return Response({'data': []})

        with patch.object(flow_media.tools_gate, 'note'), patch.object(flow_media.guard, 'content_allowed', return_value=True), \
                patch.object(urllib.request, 'urlopen', side_effect=transport):
            bot._draw('fixture', 'p:101', False, 0, 101)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].full_url, flow_media.DRAW_API)
        self.assertEqual(captured[0].get_header('Authorization'), 'Bearer ' + FAKE_KEY)
        redirected = urllib.request.HTTPRedirectHandler().redirect_request(
            captured[0], None, 302, 'moved', {}, 'https://other.invalid/image')
        self.assertIsNone(redirected.get_header('Authorization'))

    def test_all_embedding_http_paths_drop_credentials_when_redirected(self):
        captured = []

        def transport(request, **kwargs):
            captured.append(request)
            return Response({'data': [{'embedding': [1.0, 0.0]}]})

        index = sticker_index.StickerIndex.__new__(sticker_index.StickerIndex)
        index.cfg, index.model, index.url = self.cfg(route()), 'fixture', sticker_index.EMBED_API
        with patch.object(urllib.request, 'urlopen', side_effect=transport):
            self.assertEqual(index._embed_text('fixture'), [1.0, 0.0])
            self.assertEqual(sticker_index.embed_texts(index.cfg, ['fixture']), [[1.0, 0.0]])
        self.assertEqual(len(captured), 2)
        for request in captured:
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + FAKE_KEY)
            redirected = urllib.request.HTTPRedirectHandler().redirect_request(
                request, None, 302, 'moved', {}, 'https://other.invalid/embed')
            self.assertIsNone(redirected.get_header('Authorization'))


class ModelCredentialTests(unittest.TestCase):
    def test_legacy_unbound_fallback_is_never_used_by_direct_clients(self):
        client = Ark(route('https://other.invalid/v1', key=''), fallback_key='fictional-other-key')
        with patch.object(model.urllib.request, 'urlopen') as transport:
            result = client.chat([{'role': 'user', 'content': 'fixture'}])
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'missing_api_key')
        transport.assert_not_called()

    def test_chat_and_vision_pools_cannot_send_the_global_key_to_a_keyless_route(self):
        pool = ModelPool(Config(models=[route('https://other.invalid/v1', key='')],
                                api_key='fictional-global-only'))
        with patch.object(model.urllib.request, 'urlopen') as transport:
            self.assertFalse(pool.chat('fixture', [], 'fixture').ok)
            self.assertFalse(pool.vision('fixture', 'fixture', b'fake', 'image/png').ok)
        transport.assert_not_called()

    def test_model_route_key_is_used_only_on_the_original_request(self):
        captured = []

        def transport(request, **kwargs):
            captured.append(request)
            return Response({'choices': [{'message': {'content': 'fixture'}, 'finish_reason': 'stop'}]})

        with patch.object(model.urllib.request, 'urlopen', side_effect=transport):
            result = Ark(route('https://other.invalid/v1'), fallback_key='fictional-unbound').chat(
                [{'role': 'user', 'content': 'fixture'}])
        self.assertTrue(result.ok)
        self.assertEqual(captured[0].get_header('Authorization'), 'Bearer ' + FAKE_KEY)
        redirected = urllib.request.HTTPRedirectHandler().redirect_request(
            captured[0], None, 302, 'moved', {}, 'https://another.invalid/chat')
        self.assertIsNone(redirected.get_header('Authorization'))


if __name__ == '__main__':
    unittest.main()
