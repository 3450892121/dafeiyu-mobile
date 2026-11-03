import json
import ast
import importlib.util
import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
from store import Store
from main import Bot
from flow_context import ContextMixin
from flow_reply import ReplyMixin
from blacklist import policy_scope, handle_command, generation, policy_changed
from onebot import OneBot, OneBotError


class BlacklistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'bot.db')
        self.store = Store(self.path)
        self.bot = Bot.__new__(Bot)
        self.bot.store = self.store
        self.bot.cfg = {'allow_all_users': True, 'allow_users': [20], 'owner_qq': 20}
        self.bot.self_id = 99

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_persistent_validated_and_atomic(self):
        self.assertEqual([20, 30], self.store.set_blacklist([30, '20', 20]))
        for invalid in ([0], ['1x'], [True], [2**63], ['１２３']):
            with self.assertRaises(ValueError):
                self.store.set_blacklist(invalid)
            self.assertEqual([20, 30], self.store.blacklist_users())
        other = Store(self.path)
        try:
            self.assertTrue(other.is_blocked(20))
            self.store.set_blacklist([])
            self.assertFalse(other.is_blocked(20))
        finally:
            other.conn.close()

    def test_ingress_has_no_side_effect_even_for_owner(self):
        self.store.set_blacklist([20])
        self.bot.dreams = SimpleNamespace(touch=Mock())
        self.bot.q = SimpleNamespace(put_nowait=Mock())
        self.bot._parse = Mock(side_effect=AssertionError('must not parse'))
        ev = {'post_type': 'message', 'user_id': 20, 'self_id': 99, 'message_type': 'private'}
        for kind in ('private', 'group'):
            ev['message_type'] = kind
            self.assertTrue(self.bot.enqueue(ev))
            self.bot.handle(ev)
            self.assertFalse(self.bot._should_batch(ev))
        self.bot.dreams.touch.assert_not_called()
        self.bot.q.put_nowait.assert_not_called()
        self.bot._parse.assert_not_called()
        self.assertFalse(self.bot._private_allowed(20))
        self.assertFalse(self.bot.explain_gate(True, 40, 20, '', '')['reply'])

    def test_passive_and_storage_defense(self):
        self.store.set_blacklist([20])
        ContextMixin._store_passive(self.bot, 'g:40', 20, '甲', '秘密', [], [], [], {})
        self.assertEqual(0, self.store.add_message('g:40', 20, '甲', 'in', '秘密'))
        self.store.upsert_person(20, '甲')
        self.assertEqual(0, self.store.conn.execute('SELECT count(*) FROM people').fetchone()[0])

    def test_historical_context_quotes_and_other_speakers(self):
        for uid, text in [(20, '黑名单秘密'), (30, '正常消息')]:
            self.store.add_message('g:40', uid, str(uid), 'in', text, message_id=uid)
        self.store.add_message('g:40', 30, '乙', 'in', '引用说明', attachments=[{'type':'quote','who_qq':20,'text':'秘密'}])
        self.store.add_message('p:20', 99, '鲸', 'out', '曾经回答')
        self.store.set_summary('g:40', '旧摘要秘密')
        self.store.set_setting('recent_topic:g:40', '旧话题秘密')
        self.store.set_blacklist([20])
        for text in (json.dumps(self.store.messages_for_context('g:40', 20), ensure_ascii=False),
                     json.dumps(self.store.transcript('g:40', 20), ensure_ascii=False), self.store.older_text('g:40', 0)):
            self.assertIn('正常消息', text)
            self.assertNotIn('秘密', text)
        self.assertIsNone(self.store.message_by_id(20))
        self.assertEqual([], self.store.messages_for_context('p:20', 20))
        self.assertEqual('', self.store.get_session('g:40')['summary'])
        self.assertIsNone(self.store.get_setting('recent_topic:g:40'))
        self.store.set_blacklist([])
        self.assertIsNotNone(self.store.message_by_id(20))
        self.assertEqual(4, self.store.conn.execute('SELECT count(*) FROM messages').fetchone()[0])

    def test_maintenance_keeps_ignored_history_and_media_references(self):
        self.store.add_message('g:40', 20, '甲', 'in', '保留原始记录', attachments=[{'type':'image','file':'blocked.png'}])
        self.store.set_blacklist([20])
        self.store.prune()
        self.assertEqual(1, self.store.conn.execute('SELECT count(*) FROM messages').fetchone()[0])
        self.assertIn('blocked.png', self.store._referenced_media())
        self.store.set_blacklist([])
        self.assertTrue(self.store.messages_for_context('g:40', 10))

    def test_person_and_shared_memory_hidden_reversibly(self):
        self.store.add_person_memory(20, '甲最喜欢吃红烧肉', source_conv='p:20')
        self.store.add_shared_memory('甲乙一起去海边看日出', [20, 30], 40, 'event', 3, 'g:40')
        self.store.set_blacklist([20])
        self.assertEqual([], self.store.search_person_memories(20, ['红烧肉']))
        self.assertEqual([], self.store.list_shared_memories())
        self.store.set_blacklist([])
        self.assertTrue(self.store.search_person_memories(20, ['红烧肉']))
        self.assertTrue(self.store.list_shared_memories())

    def test_quote_stops_before_media_read(self):
        self.store.set_blacklist([20])
        self.bot._self_qq = lambda: 99
        self.bot.onebot = SimpleNamespace(get_msg=lambda mid: {'sender':{'user_id':20}, 'message':[{'type':'record'}]})
        self.bot._voice_note = Mock(side_effect=AssertionError('must not transcribe'))
        self.assertIsNone(self.bot._quote_info('1', 30, '乙'))
        self.bot._voice_note.assert_not_called()

    def test_forward_nodes_are_filtered_at_any_depth(self):
        self.store.set_blacklist([20])
        nodes = [{'sender':{'user_id':20,'nickname':'甲'},'content':'秘密'},
                 {'sender':{'user_id':30,'nickname':'乙'},'content':'正常内容'}]
        result = self.bot._forward_from_segs([{'type':'forward','data':{'content':nodes}}])
        self.assertIn('正常内容', result)
        self.assertNotIn('秘密', result)

    def test_queued_send_and_inflight_other_reply_cancelled(self):
        self.store.set_blacklist([20])
        self.bot.onebot = SimpleNamespace(send_private=Mock())
        self.assertFalse(ReplyMixin._send(self.bot, False, 0, 20, [{'type':'text','data':{'text':'你好'}}]))
        @policy_scope
        def generate(bot):
            snapshot = generation(bot)
            bot.store.set_blacklist([20, 30])
            self.assertEqual(snapshot, generation(bot))
            return ReplyMixin._send(bot, False, 0, 40, [{'type':'text','data':{'text':'旧上下文生成'}}])
        self.assertFalse(generate(self.bot))
        self.bot.onebot.send_private.assert_not_called()

    def test_transport_blocks_direct_private_delivery(self):
        onebot = OneBot('http://127.0.0.1:1')
        onebot.deny_user = lambda uid: uid == 20
        with self.assertRaisesRegex(OneBotError, 'blocked'):
            onebot.call('send_private_msg', user_id=20, message='不能发')

    def test_sleep_and_dream_material_exclude_blocked(self):
        now = int(time.time())
        for uid in (20, 30):
            for index in range(3):
                self.store.add_message('g:40', uid, str(uid), 'in', '考试通过成绩八十五分%d' % index, message_id=uid*100+index, ts=now-100+index)
        self.store.set_blacklist([20])
        self.assertTrue(self.store.dream_material(now))
        self.assertTrue(all(r['sender_id']==30 for r in self.store.dream_material(now)))
        self.assertTrue(all(r['sender_id']==30 for r in self.store.sleep_material(now)))

    def test_reminders_and_contact_candidates_exclude_blocked(self):
        self.store.upsert_person(20, '甲')
        self.store.upsert_person(30, '乙')
        self.store.add_reminder('g:40', int(time.time())-1, '提醒甲', target_uid=20)
        self.store.add_reminder('p:20', int(time.time())-1, '私聊提醒')
        self.store.add_reminder('g:40', int(time.time())-1, '提醒乙', target_uid=30)
        self.store.set_blacklist([20])
        self.assertEqual([30], [r['target_uid'] for r in self.store.due_reminders()])
        self.assertNotIn(20, [r['qq'] for r in self.store.people_overview([20,30], [40], 20)])
        self.bot._friends_at = time.time()
        self.bot._friends = [(20,'甲'),(30,'乙')]
        self.bot.cfg['allow_users'] = [20,30]
        self.assertEqual([30], [r['uid'] for r in self.bot._friend_candidates()])

    def test_sanitized_forward_survives_until_policy_changes(self):
        self.store.set_blacklist([20])
        self.store.add_message('g:40', 30, '乙', 'in', '正常转发', attachments=[{'type':'forward','note':'乙：正常内容','ignored_users':[20]}])
        self.assertIn('正常内容', json.dumps(self.store.messages_for_context('g:40', 10), ensure_ascii=False))
        self.store.set_blacklist([20,40])
        self.assertEqual([], self.store.messages_for_context('g:40', 10))

    def test_actual_management_handlers_auth_validation_and_clear(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('blacklist_test_console', Path(__file__).resolve().parents[2] / 'runtime/console_base.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        namespace = dict(vars(module))
        namespace['sup'] = SimpleNamespace(store=self.store, cfg={'admin_token':'synthetic-test-token'}, port=8090)
        tree = ast.parse((Path(__file__).resolve().parents[2] / 'runtime/console_base.py').read_text(encoding='utf-8'))
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name=='H')
        exec(compile(ast.Module(body=[cls], type_ignores=[]), '<actual console handlers>', 'exec'), namespace)
        handler = namespace['H'].__new__(namespace['H'])
        output=[]; handler._json=lambda code, data: output.append((code,data))
        handler._send=lambda code, *a, **kw: output.append((code,None))
        handler.path='/api/blacklist'; handler.headers={}
        handler.do_GET(); self.assertEqual(403, output[-1][0])
        handler.headers={'X-Admin-Token':'synthetic-test-token'}
        handler.do_GET(); self.assertEqual((200,{'ok':True,'users':[]}),output[-1])
        def post(value, auth=True, origin=None):
            body=json.dumps({'users':value}).encode()
            handler.headers={'Content-Length':str(len(body)), 'Content-Type':'application/json'}
            if auth: handler.headers['X-Admin-Token']='synthetic-test-token'
            if origin: handler.headers['Origin']=origin
            handler.rfile=io.BytesIO(body); handler._handle_post(); return output[-1]
        self.assertEqual(403,post([20],False)[0])
        self.assertEqual(403,post([20],origin='https://outside.invalid')[0])
        self.assertEqual((200,{'ok':True,'users':[20,30]}),post('20，30\n20'))
        self.assertEqual(400,post('bad')[0]);self.assertEqual([20,30],self.store.blacklist_users())
        self.assertEqual((200,{'ok':True,'users':[]}),post(''))

    def command(self, text, owner=20, targets=(), group=True, at_bot=True):
        self.bot.cfg['allow_groups']=[40]
        self.bot._send=Mock(return_value=True)
        segs=[{'type':'text','data':{'text':text}}]
        segs += [{'type':'at','data':{'qq':str(n)}} for n in ((99,) if at_bot else ()) + tuple(targets)]
        return handle_command(self.bot, {'message':segs}, owner, 40 if group else 0, group)

    def test_owner_can_block_and_unblock_via_at_mentions(self):
        self.assertTrue(self.command('拉黑',targets=(30,)))
        self.assertTrue(self.store.is_blocked(30))
        self.assertIn('已拉黑',self.bot._send.call_args.args[-1][0]['data']['text'])
        self.assertTrue(self.command('取消拉黑',targets=(30,)))
        self.assertFalse(self.store.is_blocked(30))

    def test_nickname_unblock_uses_management_lookup_and_ambiguity_fails(self):
        self.store.upsert_person(30,'小甲');self.store.upsert_person(40,'小乙')
        self.command('拉黑 小甲');self.assertTrue(self.store.is_blocked(30))
        self.command('取消拉黑 小甲');self.assertFalse(self.store.is_blocked(30))
        self.store.upsert_person(40,'小甲')
        self.command('拉黑 小甲');self.assertFalse(self.store.blacklist_users())

    def test_chat_controls_require_owner_and_group_at(self):
        self.assertTrue(self.command('拉黑 30',owner=40))
        self.bot._send.assert_not_called();self.assertFalse(self.store.blacklist_users())
        self.assertFalse(self.command('拉黑 30',at_bot=False))
        self.assertFalse(self.store.blacklist_users())
        self.command('拉黑 20');self.command('拉黑 99')
        self.assertFalse(self.store.blacklist_users())

    def test_private_commands_bypass_batching(self):
        self.assertTrue(self.command('拉黑 QQ30',group=False,at_bot=False))
        self.assertTrue(self.store.is_blocked(30))
        self.assertTrue(self.command('取消拉黑 30',group=False,at_bot=False))
        event={'post_type':'message','message_type':'private','user_id':20,'self_id':99,
               'message':[{'type':'text','data':{'text':'拉黑 30'}}]}
        self.assertFalse(self.bot._should_batch(event))

    def test_real_handle_consumes_owner_command_before_parsing(self):
        self.bot.cfg['allow_groups']=[40]
        self.bot._mark_seen=lambda mid: True
        self.bot._parse=Mock(side_effect=AssertionError('admin command must not reach model context'))
        self.bot._send=Mock(side_effect=lambda *a: self.assertFalse(policy_changed(self.bot)))
        event={'post_type':'message','message_type':'group','user_id':20,'self_id':99,'group_id':40,'message_id':555,
               'message':[{'type':'at','data':{'qq':'99'}},{'type':'text','data':{'text':'拉黑 '}},{'type':'at','data':{'qq':'30'}}]}
        self.bot.handle(event)
        self.assertTrue(self.store.is_blocked(30));self.bot._parse.assert_not_called()

    def test_multiple_targets_do_not_silently_pick_one(self):
        self.command('拉黑',targets=(30,40))
        self.assertFalse(self.store.blacklist_users())


if __name__ == '__main__':
    unittest.main()
