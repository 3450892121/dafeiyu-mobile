"""Group observer uses the real current account's relation; all transports are fake."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import flow_companion
from onebot import text_seg
from relationship_policy import attitude_context
from store import Store


class CompanionRelationshipTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, *, growth=True, unified=True, act='quiet'):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as folder:
            store = Store(str(Path(folder) / 'bot.db'))
            try:
                bot = SimpleNamespace(
                    cfg={'self_growth_enabled': growth, 'owner_qq': 90,
                         'bot_name': '合成机器人', 'sticker_judge_enabled': False},
                    self_id=99, store=store, stickers=[],
                    persona=SimpleNamespace(unified_self=unified,
                        system_prompt=lambda *a, **k: '合成人设'),
                    pool=SimpleNamespace(chat=Mock(return_value=SimpleNamespace(
                        ok=True, text=json.dumps({'act': act, 'text': '先看具体情况。'},
                                                ensure_ascii=False), tool_calls=[]))),
                    _companion_enabled=lambda *a: True,
                    _is_group_speak_quiet=lambda: False,
                    _group_speak_last_from_bot=lambda *a: False,
                    _proactive_now_line=lambda: '今天',
                    _companion_state=SimpleNamespace(snapshot=lambda *a: (3, True, True),
                        eligible=lambda *a: True, sent=Mock()),
                    _tool_defs=lambda *a, **k: [], _json_from_text=json.loads,
                    _conv_lock=lambda *a: contextlib.nullcontext(),
                    _text_segs=lambda text: [text_seg(text)],
                    _send=Mock(return_value=True), send_sticker=Mock(),
                    _sticker_label=lambda *a: '[表情]')
                with patch.object(flow_companion.reply_autonomy, 'resting', return_value=False), \
                     patch('scene_continuity.context', return_value=''), \
                     patch.dict('sys.modules', {'recent_topic': SimpleNamespace(
                         read=lambda *a: '', schedule=lambda *a: None)}):
                    yield bot
            finally:
                store.conn.close()

    def relation(self, store, uid, favor, trust):
        store.conn.execute('INSERT INTO person_relations(qq,favor,trust,reason,updated_at) '
                           'VALUES(?,?,?,?,?)', (uid, favor, trust, '合成隐私原因', int(time.time())))
        store.conn.commit()

    def run_companion(self, bot):
        with patch.object(flow_companion, 'attitude_context', wraps=attitude_context) as relation:
            flow_companion.CompanionMixin._companion_run.__wrapped__(bot, 7, 3)
        bot.pool.chat.assert_called_once()
        logs = bot.store.recent_logs()
        self.assertTrue(any(row['tag'] == 'companion' and
                            row['detail'].startswith(('群7 reply ', '群7 quiet ')) for row in logs))
        self.assertFalse(any('判断失败' in row['detail'] for row in logs))
        return bot.pool.chat.call_args.args[0], relation

    def test_low_relation_reaches_both_decision_and_draft_in_all_modes(self):
        for growth, unified in ((False, False), (True, False), (False, True), (True, True)):
            for act in ('reply', 'quiet'):
                with self.subTest(growth=growth, unified=unified, act=act), \
                     self.fixture(growth=growth, unified=unified, act=act) as bot:
                    self.relation(bot.store, 20, 4.1, 10)
                    bot.store.add_message('g:7', 20, '合成群友', 'in', '这个问题请帮我看看', '', 101)
                    system, relation = self.run_companion(bot)
                    relation.assert_called_once_with(bot, 20)
                    self.assertIn(attitude_context(bot, 20), system)
                    self.assertIn('判断reply/quiet和起草接话都参考', system)
                    self.assertIn('认真具体的求助不因关系自动拒绝', system)
                    if act == 'quiet':
                        bot._send.assert_not_called()
                        bot._companion_state.sent.assert_not_called()
                    else:
                        bot._send.assert_called_once()
                        bot._companion_state.sent.assert_called_once_with(7, True)
                        self.assertEqual(bot._send.call_args.args[-1], [text_seg('先看具体情况。')])
                        outgoing = [r for r in bot.store.thread_rows('g:7', 10) if r['direction'] == 'out']
                        self.assertEqual([r['text'] for r in outgoing], ['先看具体情况。'])

    def test_same_nickname_quote_and_forward_do_not_replace_the_real_author(self):
        with self.fixture() as bot:
            self.relation(bot.store, 20, 4.1, 10)
            self.relation(bot.store, 21, 90, 90)
            bot.store.add_message('g:7', 21, '相同昵称', 'in', '合成旧发言', '', 100)
            bot.store.add_message('g:7', 20, '相同昵称', 'in', '他说的这个问题呢', '', 101,
                [{'type': 'quote', 'who_qq': 21, 'who_name': '相同昵称', 'text': '合成引用'},
                 {'type': 'forward', 'sender_id': 21, 'text': '合成转发'}])
            bot.store.add_person_memory(20, '私聊保密代号合成紫色海星', 'fact', 4,
                                       source_conv='p:20', visibility='private')
            system, relation = self.run_companion(bot)
            relation.assert_called_once_with(bot, 20)
            self.assertIn('当前真实发言账号QQ20', system)
            self.assertIn('不套给全群', system)
            self.assertIn(attitude_context(bot, 20), system)
            self.assertNotIn(attitude_context(bot, 21), system)
            self.assertNotIn('合成隐私原因', system)
            self.assertNotIn('合成紫色海星', system)

    def test_unknown_author_uses_own_default_not_the_previous_speakers_low_relation(self):
        with self.fixture() as bot:
            self.relation(bot.store, 20, 4.1, 10)
            bot.store.add_message('g:7', 20, '相同昵称', 'in', '合成旧发言', '', 100)
            bot.store.add_message('g:7', 22, '相同昵称', 'in', '这是一个新话题', '', 101)
            system, relation = self.run_companion(bot)
            relation.assert_called_once_with(bot, 22)
            self.assertIn('当前真实发言账号QQ22', system)
            self.assertIn(attitude_context(bot, 22), system)
            self.assertNotIn(attitude_context(bot, 20), system)
            self.assertIsNone(bot.store.get_person_relation(22))


if __name__ == '__main__':
    unittest.main()
