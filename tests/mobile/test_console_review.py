"""Console requests use synthetic identities and this run's temporary loopback server."""
import ast
from contextlib import closing
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import console_base as panel
from config import Config as RuntimeConfig
from ops_api import OpsApi


class Config(dict):
    def set(self, key, value):
        self[key] = value


class ConsoleReviewTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(admin_token='fictional-console-review-token', login_uin='190001',
                          owner_qq=190002, api_key='fictional-unscoped-key',
                          allow_all_users=False)
        self.store = Mock()
        self.store.self_snapshot.return_value = {'ok': True, 'items': []}
        self.store.get_profile.return_value = {'name': '临时管理员'}
        self.sup = SimpleNamespace(cfg=self.cfg, store=self.store, port=33114)
        source = Path(panel.__file__).read_text(encoding='utf-8')
        main = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'main')
        handler = next(n for n in main.body if isinstance(n, ast.ClassDef) and n.name == 'H')
        self.ark = Mock()
        self.ark.return_value.chat.return_value = SimpleNamespace(ok=False, text='', error='synthetic failure')
        namespace = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, sup=self.sup, json=json,
                         hmac=hmac, parse_qs=parse_qs, urlsplit=urlsplit,
                         current_self_state=panel.current_self_state, apply_settings=panel.apply_settings,
                         Ark=self.ark, time=panel.time)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), '<review-console-http>', 'exec'), namespace)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), namespace['H'])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, fields=None, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            headers = {'Content-Type': 'application/x-www-form-urlencoded',
                       'X-Admin-Token': self.cfg.get('admin_token')}
            if origin:
                headers['Origin'] = origin
            connection.request(method, path, urlencode(fields) if fields is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_configured_administrator_does_not_confirm_a_family_relationship(self):
        status, result = self.request('GET', '/api/self-state')
        self.assertEqual(status, 200)
        self.assertNotIn('family', result)
        self.assertEqual(result['administrator'], {'configured': True, 'profile_available': True})
        self.cfg['owner_qq'] = 0
        self.store.get_profile.reset_mock()
        status, result = self.request('GET', '/api/self-state')
        self.assertFalse(result['administrator']['configured'])
        self.assertFalse(result['administrator']['profile_available'])
        self.store.get_profile.assert_not_called()

    def test_foreign_origin_receives_readable_403_without_applying_posted_changes(self):
        before = dict(self.cfg)
        for _ in range(12):
            status, result = self.request('POST', '/api/save',
                                          {'allow_all_users': '1', 'padding': 'x' * 8192},
                                          origin='https://foreign.example.invalid')
            self.assertEqual((status, result['error']), (403, 'origin'))
        self.assertEqual(self.cfg, before)
        self.store.assert_not_called()

    def test_invalid_or_same_account_administrator_does_not_partially_save(self):
        before = dict(self.cfg)
        for owner in ('190001', 'invalid', '012345', '1234', '1' * 13):
            with self.subTest(owner=owner):
                status, result = self.request('POST', '/api/save',
                                              {'owner_qq': owner, 'allow_all_users': '1'})
                self.assertEqual(status, 500)
                self.assertFalse(result['ok'])
                self.assertEqual(self.cfg, before)
        self.assertEqual(self.request('POST', '/api/save', {'owner_qq': '190003'})[0], 200)
        self.assertEqual(self.cfg['owner_qq'], 190003)
        self.assertEqual(self.request('POST', '/api/save', {'owner_qq': ''})[0], 200)
        self.assertEqual(self.cfg['owner_qq'], 0)

    def test_editing_model_destination_does_not_reuse_previous_service_key(self):
        original = {'id': 'm1', 'name': 'temporary', 'base_url': 'https://one.example.invalid/v1',
                    'model': 'temporary', 'api_key': 'fictional-line-key', 'enabled': True}
        for base, expected in (('https://one.example.invalid/v1/', 'fictional-line-key'),
                               ('https://other.example.invalid/v1', ''),
                               ('https://one.example.invalid/another-api', ''),
                               ('http://one.example.invalid/v1', '')):
            for masked in ('', '***'):
                with self.subTest(base=base, masked=masked):
                    self.cfg['models'] = [dict(original)]
                    changed = dict(original, base_url=base, api_key=masked)
                    status, result = self.request('POST', '/api/models', {'models': json.dumps([changed])})
                    self.assertEqual((status, result['ok']), (200, True))
                    self.assertEqual(self.cfg['models'][0]['api_key'], expected)
                    self.assertEqual(result['models'][0]['api_key_set'], bool(expected))
                    self.assertNotIn('api_key', result['models'][0])
                    self.assertNotIn('fictional-line-key', json.dumps(result))

    def test_image_limit_checkbox_really_persists_both_off_and_on(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'config.json')
            self.cfg = RuntimeConfig(path)
            self.sup.cfg = self.cfg
            self.cfg.set('image_watch_unlimited', True)
            for value, expected in (('0', False), ('1', True)):
                status, result = self.request('POST', '/api/save', {'image_watch_unlimited': value})
                self.assertEqual((status, result['ok']), (200, True))
                self.assertIs(RuntimeConfig(path).get('image_watch_unlimited'), expected)

    def test_console_vision_does_not_fill_an_unscoped_key_into_a_model(self):
        line = {'id': 'm1', 'base_url': 'https://one.example.invalid/v1', 'model': 'temporary',
                'enabled': True, 'api_key': ''}
        self.cfg['models'] = [line]
        with patch.object(panel, 'Ark') as ark:
            self.assertIsNone(panel._vision_client(self.cfg))
            ark.assert_not_called()
            line['api_key'] = 'fictional-line-key'
            self.assertIs(panel._vision_client(self.cfg), ark.return_value)
            self.assertNotIn('fallback_key', ark.call_args.kwargs)

    def test_console_model_test_does_not_borrow_the_global_key(self):
        self.cfg['models'] = [{'id': 'm1', 'model': 'temporary',
                               'base_url': 'https://one.example.invalid/v1', 'api_key': ''}]
        self.assertEqual(self.request('POST', '/api/model_test', {'id': 'm1'})[0], 200)
        self.assertEqual(self.ark.call_args.args[1], '')


class MobileOperationsReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'data'
        self.data.mkdir()
        self.cfg = {'bot_name': '临时实例', 'login_uin': '190001',
                    'admin_port': 33114, 'webhook_port': 33113, 'napcat_webui_port': 33112}
        self.ops = OpsApi(str(self.data), str(self.home), self.cfg)

    def test_missing_files_produce_a_visible_warning_not_empty_success(self):
        with patch('ops_api._port_open', return_value=False), patch.object(self.ops, '_live_checks', return_value={'persona': False}):
            result = self.ops.selftest()
        self.assertTrue(result['ok'])
        self.assertFalse(result['passed'])
        report = result['result']
        self.assertEqual(report['overall'], 'warn')
        self.assertFalse(report['bots'][0]['ok'])
        self.assertFalse(report['bots'][0]['checks']['database'])
        self.assertIn('bot.db', report['bots'][0]['problems'])
        self.assertEqual(self.ops.health(), report)
        self.assertFalse((self.data / 'bot.db').exists())

    def test_healthy_local_checks_return_the_frontend_result_and_persist_it(self):
        with closing(sqlite3.connect(self.data / 'bot.db')) as connection:
            connection.execute('CREATE TABLE synthetic_evidence(value TEXT)')
            connection.commit()
        (self.data / 'config.json').write_text('{}', encoding='utf-8')
        with patch('ops_api._port_open', return_value=True), patch.object(self.ops, '_live_checks', return_value={'persona': True}):
            result = self.ops.selftest()
        self.assertTrue(result['passed'])
        self.assertEqual(result['result']['overall'], 'ok')
        self.assertEqual(len(result['result']['bots']), 1)
        self.assertTrue(result['result']['bots'][0]['ok'])
        self.assertEqual(self.ops.health(), result['result'])
        self.assertFalse(self.ops.mail()['supported'])
        self.assertFalse(self.ops.mail_test()['ok'])


if __name__ == '__main__':
    unittest.main()
