"""Read-only pipeline evidence, synthetic contacts/events and no live connections."""
import json
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mobile_diagnostics import HookHealth, pipeline_status
from mobile_session import login_runtime_status


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'data'
        self.data.mkdir()
        self.cfg = Mock()
        settings = {'owner_qq': 190002, 'allow_users': [190002], 'allow_groups': [190003],
                    'login_uin': '190001', 'onebot_token': 'fictional-secret-never-return'}
        self.cfg.get.side_effect = lambda key: settings.get(key)
        self.now = int(time.time())
        state = self.data / 'status.json'
        state.write_text(json.dumps({'pid': os.getpid(), 'uptime': 600}))
        os.utime(state, (self.now - 3600, self.now - 3600))
        self.since = self.now - 4205
        con = sqlite3.connect(self.data / 'bot.db')
        con.executescript('CREATE TABLE messages(conv TEXT,direction TEXT,ts INTEGER,text TEXT);'
                          'CREATE TABLE logs(ts INTEGER,tag TEXT,detail TEXT);'
                          'CREATE TABLE usage_records(ts INTEGER,ok INTEGER);')
        con.executemany('INSERT INTO messages VALUES(?,?,?,?)', [
            ('p:190002', 'in', self.now - 4100, 'private content must stay private'),
            ('p:190002', 'out', self.now - 4000, 'secret reply'),
            ('g:190003', 'in', self.now - 4000, 'group content'),
            ('p:190099', 'in', self.now - 4000, 'unconfigured contact'),
            ('p:190002', 'in', self.now - 5000, 'old message')])
        con.execute('INSERT INTO logs VALUES(?,?,?)', (self.now - 4000, 'autonomy', 'p:190002 quiet rest private details'))
        con.execute('INSERT INTO usage_records VALUES(?,?)', (self.now - 4000, 1))
        con.commit()
        con.close()

    def test_stopped_brain_window_uses_snapshot_time_and_counts_scoped_data_only(self):
        result = pipeline_status(self.home, self.cfg)
        self.assertTrue(result['database_available'])
        self.assertEqual(result['since'], self.since)
        self.assertEqual(len(result['conversations']), 2)
        private, group = result['conversations']
        self.assertEqual((private['in'], private['out'], private['quiet']), (1, 1, 1))
        self.assertEqual((group['in'], group['out']), (1, 0))
        self.assertEqual(result['model_ok'], 1)
        text = json.dumps(result)
        for secret in ('private content', 'secret reply', 'private details', '190099', 'fictional-secret'):
            self.assertNotIn(secret, text)

    def test_diagnosis_keeps_database_bytes_and_mtime_unchanged(self):
        path = self.data / 'bot.db'
        before = path.read_bytes(), path.stat().st_mtime_ns
        pipeline_status(self.home, self.cfg)
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_missing_database_is_not_created_or_reported_as_zero_success(self):
        (self.data / 'bot.db').unlink()
        result = pipeline_status(self.home, self.cfg)
        self.assertFalse(result['database_available'])
        self.assertFalse((self.data / 'bot.db').exists())

    def test_callback_secret_is_compared_without_returning_it(self):
        path = self.home / 'Napcat/opt/QQ/resources/app/app_launcher/napcat/config/onebot11_190001.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'network': {'httpClients': [{'enable': True, 'url': 'http://127.0.0.1:33113/',
                                                               'token': self.cfg.get('onebot_token')}]}}))
        result = pipeline_status(self.home, self.cfg)
        self.assertTrue(result['callback_configured'])
        self.assertTrue(result['callback_token_matches'])
        self.assertNotIn(self.cfg.get('onebot_token'), json.dumps(result))

    def test_old_errors_do_not_become_current_error_evidence(self):
        old = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.since - 100))
        recent = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.since + 100))
        (self.data / 'bot.log').write_text(old + ' [worker] EXC ModuleNotFoundError\n' +
                                         recent + ' [worker] EXC TypeError private log details\n')
        result = pipeline_status(self.home, self.cfg)
        self.assertIn('消息处理异常', result['recent_flags'])
        self.assertNotIn('缺少运行模块', result['recent_flags'])
        self.assertNotIn('private log details', json.dumps(result))

    def test_telemetry_counts_concurrent_events_without_copying_them(self):
        health = HookHealth(self.data)
        event = {'post_type': 'message', 'message_type': 'private', 'text': 'never persist message', 'token': 'never persist credential'}
        workers = [threading.Thread(target=health.record, args=(204, event)) for _ in range(12)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        path = self.data / 'mobile-hook-health.json'
        text = path.read_text()
        value = json.loads(text)
        self.assertEqual(value['statuses']['204'], 12)
        self.assertEqual(value['messages']['private'], 12)
        self.assertNotIn('never persist', text)
        self.assertEqual(pipeline_status(self.home, self.cfg)['callbacks'], {'204': 12})
        value['pid'] += 1
        path.write_text(json.dumps(value))
        self.assertEqual(pipeline_status(self.home, self.cfg)['callbacks'], {})

    def test_telemetry_write_failure_is_nonfatal(self):
        with patch('mobile_diagnostics.os.replace', side_effect=OSError('synthetic write failure')):
            HookHealth(self.data).record(401)
        self.assertFalse(list(self.data.glob('.hook-health-*')))

    def test_runtime_probe_reports_initialization_and_verification_without_raw_payload(self):
        path = self.home / 'Napcat/opt/QQ/resources/app/app_launcher/napcat/config/webui.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"token":"fictional-webui-secret"}')
        data = {'code': 0, 'data': {'isLogin': False, 'isOffline': False, 'coreReady': False,
                                   'loginPhase': 'initializing', 'qrcodeurl': 'private-qr-must-not-return',
                                   'loginError': '安全验证 ticket=private-ticket-must-not-return'}}
        responses = [io.BytesIO(b'{"data":{"Credential":"fictional-credential"}}'),
                     io.BytesIO(json.dumps(data).encode())]
        with patch('mobile_session.urllib.request.urlopen', side_effect=responses) as request:
            result = login_runtime_status(self.home, 33112)
        self.assertEqual(result['phase'], 'initializing')
        self.assertTrue(result['available'])
        self.assertTrue(result['verification_required'])
        self.assertFalse(result['core_ready'])
        for call in request.call_args_list:
            self.assertNotIn('SetQuickLogin', call.args[0].full_url)
            self.assertNotIn('RefreshQRcode', call.args[0].full_url)
        self.assertTrue(request.call_args_list[-1].args[0].full_url.endswith('/CheckLoginStatus'))
        for secret in ('private-qr', 'private-ticket', 'fictional-credential', 'fictional-webui-secret'):
            self.assertNotIn(secret, json.dumps(result))

    def test_unavailable_runtime_probe_does_not_claim_logged_out(self):
        result = login_runtime_status(self.home, 33112)
        self.assertFalse(result['available'])
        self.assertEqual(result['phase'], 'unknown')


if __name__ == '__main__':
    unittest.main()
