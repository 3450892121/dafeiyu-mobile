"""Unsolicited openings respect effective relations and a choice to stay quiet."""
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flow_proactive import ProactiveMixin
from store import Store


class ProactiveRelationshipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / 'bot.db'))
        self.addCleanup(self.store.conn.close)
        self.now = int(time.time())
        self.bot = ProactiveMixin()
        self.bot.cfg = {'owner_qq': 10, 'allow_users': [20, 30],
                        'self_growth_enabled': False, 'reply_autonomy_enabled': False}
        self.bot.store = self.store
        self.bot.self_id = 99
        self.bot.onebot = SimpleNamespace(get_friend_list=Mock(return_value=[]))
        self.bot.persona = SimpleNamespace(system_prompt=Mock(return_value='当前实例身份'),
                                           relation_context=Mock(return_value='已确认账号权限'))
        self.bot._proactive_now = lambda: self.now
        self.bot.pool = SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True, text='')))
        self.bot._send = Mock()

    def relation(self, uid, favor, trust, age=0):
        self.store.conn.execute(
            'INSERT OR REPLACE INTO person_relations(qq,favor,trust,updated_at) VALUES(?,?,?,?)',
            (uid, favor, trust, self.now - age))
        self.store.conn.commit()

    def cand(self):
        return {'conv': 'p:20', 'uid': 20, 'nick': '同名群友', 'is_owner': False,
                'activity': self.now - 3600}

    def test_candidates_keep_real_zero_and_use_effective_scores(self):
        self.relation(20, 0, 0)
        self.relation(30, 90, 90, age=180 * 86400)
        candidates = {item['uid']: item for item in self.bot._friend_candidates()}
        self.assertEqual((0, 0), (candidates[20]['favor'], candidates[20]['trust']))
        effective = self.store.get_person_relation_effective(30)
        self.assertEqual(effective['favor'], candidates[30]['favor'])
        self.assertEqual(effective['trust'], candidates[30]['trust'])
        self.assertLess(candidates[30]['favor'], 90)
        self.assertEqual((50, 30), (candidates[10]['favor'], candidates[10]['trust']))

    def test_personal_context_contains_distance_without_private_evidence(self):
        self.relation(20, 4.1, 10)
        self.store.conn.execute('UPDATE person_relations SET reason=? WHERE qq=20',
                                ('不可公开的私聊事件',))
        self.store.conn.commit()
        for growth in (False, True):
            with self.subTest(growth=growth):
                self.bot.cfg['self_growth_enabled'] = growth
                system = self.bot._proactive_personal_context('身份', 'p:20', 20)
                self.assertIn('明显疏远', system)
                self.assertIn('明显戒备', system)
                self.assertIn('认识很久不能抵消', system)
                self.assertNotIn('不可公开的私聊事件', system)
                self.assertNotIn('4.1', system)

    def test_group_opening_with_no_person_never_looks_up_personal_relation(self):
        with patch.object(self.store, 'get_person_relation_effective',
                          side_effect=AssertionError('群话题不应读取某人的关系')) as lookup:
            system, prompt = self.bot._group_speak_prompt(
                {'conv': 'g:40', 'name': '合成群'}, 90)
        lookup.assert_not_called()
        self.assertNotIn('明显疏远', system)
        self.assertIn('不要 @ 任何人', prompt)

    def test_successful_quiet_choice_is_not_forced_to_generate_again(self):
        self.relation(20, 4.1, 10)
        for raw in ('  ', '""', '“ ”'):
            with self.subTest(raw=raw):
                self.bot.pool.chat.reset_mock()
                self.bot.pool.chat.return_value = SimpleNamespace(ok=True, text=raw)
                cand = self.cand()
                self.assertEqual('', self.bot._proactive_generate(cand, 60, {'material': '普通闲聊'}))
                self.bot.pool.chat.assert_called_once()
                self.assertTrue(cand['_proactive_declined'])
        system, _, prompt = self.bot.pool.chat.call_args.args
        self.assertIn('明显疏远', system)
        self.assertIn('空字符串', prompt)
        self.assertIn('不取消已承诺的提醒', prompt)

    def test_declined_opening_never_sends_or_counts_a_failure(self):
        self.relation(20, 4.1, 10)
        contact = SimpleNamespace(check=Mock(return_value=True))
        with patch('flow_proactive.private_contact.attempt', return_value=contact), \
             patch.object(self.store, 'add_log') as add_log:
            result = self.bot._proactive_send(self.cand(), '2026-11-04', 60)
        self.assertEqual({'ok': False, 'reason': '本轮不想主动联系'}, result)
        self.bot._send.assert_not_called()
        add_log.assert_not_called()
        self.assertEqual(0, self.bot._proactive_count('p:20', '2026-11-04'))

    def test_model_error_is_failure_instead_of_a_personal_choice(self):
        self.relation(20, 4.1, 10)
        contact = SimpleNamespace(check=Mock(return_value=True))
        self.bot.pool.chat.return_value = SimpleNamespace(ok=False, text='')
        with patch('flow_proactive.private_contact.attempt', return_value=contact), \
             patch.object(self.store, 'add_log') as add_log:
            result = self.bot._proactive_send(self.cand(), '2026-11-04', 60)
        self.assertEqual('生成失败', result['reason'])
        self.assertFalse(result['ok'])
        self.bot._send.assert_not_called()
        add_log.assert_called_once()

    def test_nonempty_output_cleaned_away_is_failure_not_a_quiet_choice(self):
        self.relation(20, 4.1, 10)
        contact = SimpleNamespace(check=Mock(return_value=True))
        for raw in ('[脸:微笑]', '我：', '“[脸:微笑]”'):
            with self.subTest(raw=raw):
                self.bot.pool.chat.return_value = SimpleNamespace(ok=True, text=raw)
                cand = self.cand()
                with patch('flow_proactive.private_contact.attempt', return_value=contact), \
                     patch.object(self.store, 'add_log') as add_log:
                    result = self.bot._proactive_send(cand, '2026-11-04', 60)
                self.assertEqual('生成失败', result['reason'])
                self.assertFalse(cand['_proactive_declined'])
                self.bot._send.assert_not_called()
                add_log.assert_called_once()

    def test_model_exception_does_not_reuse_an_earlier_quiet_marker(self):
        cand = self.cand()
        cand['_proactive_declined'] = True
        self.bot.pool.chat.side_effect = TimeoutError('synthetic timeout')
        self.assertEqual('', self.bot._proactive_generate(cand, 60))
        self.assertNotIn('_proactive_declined', cand)


if __name__ == '__main__':
    unittest.main()
