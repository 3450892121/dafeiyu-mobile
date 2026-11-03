"""Failure replies use real send branches and an isolated history database; no network."""
import ast
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
APP = Path(__file__).resolve().parents[2] / 'runtime' / 'core'
_TEST_HOME = tempfile.TemporaryDirectory(prefix='failure-receipt-home-', dir=os.getcwd())
_PREVIOUS_HOME = os.environ.get('DAFEIYU_HOME')
os.environ['DAFEIYU_HOME'] = _TEST_HOME.name


def cleanup_home():
    if _PREVIOUS_HOME is None:
        os.environ.pop('DAFEIYU_HOME', None)
    else:
        os.environ['DAFEIYU_HOME'] = _PREVIOUS_HOME
    _TEST_HOME.cleanup()


unittest.addModuleCleanup(cleanup_home)
sys.path.insert(0, str(APP))
from onebot import OneBotError, text_seg
from store import Store


class FailureReceiptTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket, 'create_connection', side_effect=AssertionError('No network in receipt tests'))
        network.start()
        self.addCleanup(network.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.store = Store(os.path.join(self.tmp.name, 'bot.db'))
        self.tree = ast.parse((APP / 'flow_reply.py').read_text(encoding='utf-8-sig'))
        self.functions = {node.name: node for node in ast.walk(self.tree)
                          if isinstance(node, ast.FunctionDef)}
        self.guard = SimpleNamespace(scrub=lambda text: text, strip_image_notes=lambda text: text,content_allowed=lambda *args:True,
                                     check=Mock(return_value={'action': 'ok'}))
        self.namespace = {'text_seg': text_seg, 'log': Mock(), 'guard': self.guard,
                          'blocked': lambda *args: False, 'policy_changed': lambda *args: False,
                          'is_connection_refused': lambda error: False, 'OneBotError': OneBotError,
                          'time': SimpleNamespace(sleep=Mock()), 'note_delivery': Mock()}
        names = ['_send_failure_receipt', '_send']
        if '_send_detail' in self.functions:
            names.append('_send_detail')
        exec(compile(ast.Module(body=[self.functions[name] for name in names], type_ignores=[]),
                     '<actual-failure-receipt-and-sender>', 'exec'), self.namespace)
        self.send = Mock(return_value={'message_id': 77})
        self.bot = SimpleNamespace(store=self.store, cfg={'bot_name': '测试鲸'}, self_id=9,
                                   onebot=SimpleNamespace(send_private=self.send, send_group=self.send),
                                   _text_segs=lambda text: [text_seg(text)], _take_group_at=lambda *args: None)
        for name in names:
            setattr(self.bot, name, MethodType(self.namespace[name], self.bot))

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def receipt(self, text='这次没想好，等会儿再试。', reason='model_fail', conv='p:1'):
        return self.bot._send_failure_receipt(conv, conv.startswith('g:'), 3, 1, text, reason)

    def history(self):
        return self.store.conn.execute("SELECT conv,direction,text FROM messages WHERE direction='out'").fetchall()

    def receipt_logs(self):
        return self.store.conn.execute("SELECT detail,ok FROM logs WHERE tag='reply_failure_sent'").fetchall()

    def test_successful_receipt_is_visible_once_in_context_and_session(self):
        self.assertTrue(self.receipt())
        self.send.assert_called_once()
        rows = self.history()
        self.assertEqual([('p:1', 'out', '这次没想好，等会儿再试。')], [tuple(row) for row in rows])
        self.assertTrue(self.store.get_session('p:1')['last_active'])
        self.assertIn('这次没想好', self.store.messages_for_context('p:1', 5, self_id=9)[0]['content'])
        self.assertEqual([('conv=p:1 reason=model_fail', 1)], [tuple(row) for row in self.receipt_logs()])

    def test_unknown_send_failure_is_not_replayed_or_recorded_as_sent(self):
        self.send.side_effect = TimeoutError('uncertain delivery fixture')
        self.assertFalse(self.receipt())
        self.send.assert_called_once()
        self.assertEqual([], self.history())
        self.assertEqual([], self.receipt_logs())
        self.assertEqual(0, self.store.get_session('p:1')['last_active'])

    def test_outbound_guard_can_block_receipt_before_transport_or_history(self):
        self.guard.check.return_value = {'action': 'drop', 'reason': 'fixture blocked'}
        self.assertFalse(self.receipt())
        self.send.assert_not_called()
        self.assertEqual([], self.history())
        self.assertEqual([], self.receipt_logs())

    def test_history_failure_does_not_resend_confirmed_receipt(self):
        with patch.object(self.store, 'add_message', side_effect=RuntimeError('history fixture')):
            self.assertTrue(self.receipt())
        self.send.assert_called_once()
        self.assertEqual([], self.history())
        self.namespace['log'].assert_called_once()

    def test_blank_receipt_does_not_send_or_create_session(self):
        self.assertFalse(self.receipt('  '))
        self.send.assert_not_called()
        self.assertEqual([], self.history())
        self.assertEqual([], self.receipt_logs())
        self.assertEqual(0, self.store.get_session('p:1')['last_active'])

    def test_failure_exit_branches_send_and_record_one_receipt_each(self):
        cases = [('tool_reply', '这句没组织好，等我缓口气再说。'),
                 ('model_fail', '这次没想好，等会儿再试。')]
        if 'reply_format' in ast.unparse(self.functions['_reply']):
            cases.append(('reply_format', '这句没组织好，等我缓口气再说。'))
        for index, (reason, text) in enumerate(cases):
            with self.subTest(reason=reason):
                matches = []
                for node in ast.walk(self.functions['_reply']):
                    if not isinstance(node, ast.If):
                        continue
                    for body in (node.body, node.orelse):
                        if any(isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                               and isinstance(stmt.value.func, ast.Attribute)
                               and stmt.value.func.attr == '_send_failure_receipt'
                               and any(isinstance(arg, ast.Constant) and arg.value == reason
                                       for arg in stmt.value.args) for stmt in body):
                            matches.append(body)
                self.assertEqual(1, len(matches))
                wrapper = ast.parse('def run(self, conv, is_group, gid, uid, r):\n    pass').body[0]
                wrapper.body = matches[0]
                module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
                exec(compile(module, '<actual-failure-exit>', 'exec'), self.namespace)
                self.namespace['run'](self.bot, 'g:%d' % (index + 3), True, index + 3, 1,
                                      SimpleNamespace(error='synthetic model error'))
                self.assertEqual(index + 1, self.send.call_count)
                self.assertEqual(index + 1, len(self.history()))
                self.assertEqual(text, self.history()[-1]['text'])
                self.assertEqual(index + 1, len(self.receipt_logs()))

    @unittest.skipUnless('_send_detail' in (APP / 'flow_reply.py').read_text(encoding='utf-8-sig'),
                         'Partial delivery metadata belongs to the official bridge')
    def test_partial_delivery_never_records_an_entire_failure_receipt(self):
        self.send.side_effect = OneBotError('partial fixture', {'delivered_text': '这次'})
        self.assertFalse(self.receipt())
        self.send.assert_called_once()
        self.assertEqual([], self.history())
        self.assertEqual([], self.receipt_logs())


if __name__ == '__main__':
    unittest.main()
