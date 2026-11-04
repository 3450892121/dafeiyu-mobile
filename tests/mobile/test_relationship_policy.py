"""Real temporary relationships and choice pipeline; every model response is synthetic."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import reply_autonomy
from relationship_policy import attitude_context, relation_state
from store import Store


class RelationshipPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='relationship-policy-')
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / 'test.db'))
        self.addCleanup(self.store.conn.close)
        self.bot = SimpleNamespace(
            store=self.store, cfg={'owner_qq': 20, 'bot_name': '纳西妲'}, self_id=99,
            persona=SimpleNamespace(unified_self=False),
            pool=SimpleNamespace(chat=Mock(return_value=SimpleNamespace(
                ok=True, text='{"action":"reply","mode":"engaged","minutes":1,"reason":"help"}'))))

    def relation(self, uid=30, favor=4.1, trust=10, age=0):
        self.store.conn.execute(
            'INSERT OR REPLACE INTO person_relations(qq,favor,trust,reason,updated_at) VALUES(?,?,?,?,?)',
            (uid, favor, trust, '仅限私聊的关系原因', int(time.time()) - age))
        self.store.conn.commit()

    def choose(self, uid=30, quote=None):
        return reply_autonomy.choose(self.bot, 'g:40', uid, '同名群友', uid == 20,
                                     True, '请帮我算十二乘十三', {}, quote=quote, trigger='at')

    def test_effective_scores_match_reply_decay_and_keep_fractional_zero_values(self):
        for favor, trust, age in ((4.1, 10, 0), (0, 0, 0), (0, 90, 90 * 86400)):
            with self.subTest(favor=favor, age=age):
                self.relation(favor=favor, trust=trust, age=age)
                effective = self.store.get_person_relation_effective(30, 90, 180)
                self.assertEqual({k: effective[k] for k in ('favor', 'trust')}, relation_state(self.bot, 30))

    def test_fractional_scores_do_not_round_up_across_attitude_thresholds(self):
        for favor, trust, expected in ((19.6, 50, '明显疏远'), (39.6, 50, '有距离'),
                                       (80, 14.6, '明显戒备'), (80, 29.6, '有戒心')):
            with self.subTest(favor=favor, trust=trust):
                self.relation(favor=favor, trust=trust)
                self.assertIn(expected, attitude_context(self.bot, 30))

    def test_context_reads_only_scores_and_never_changes_or_exports_private_records(self):
        self.relation()
        before = self.store.get_person_relation(30)
        changes = self.store.conn.total_changes
        context = attitude_context(self.bot, 30)
        self.assertIn('明显疏远', context)
        self.assertIn('明显戒备', context)
        self.assertNotIn('4.1', context)
        self.assertNotIn('仅限私聊的关系原因', context)
        self.assertEqual(before, self.store.get_person_relation(30))
        self.assertEqual(changes, self.store.conn.total_changes)

    def test_unseen_people_keep_neutral_defaults_without_creating_a_relationship(self):
        self.assertEqual({'favor': 50.0, 'trust': 30.0}, relation_state(self.bot, 31))
        context = attitude_context(self.bot, 31)
        self.assertIn('普通相处', context)
        self.assertNotIn('明显疏远', context)
        self.assertIsNone(self.store.get_person_relation(31))

    def test_whole_group_without_a_person_does_not_acquire_someones_attitude(self):
        with patch.object(self.store, 'get_person_relation_effective') as read:
            self.assertEqual('', attitude_context(self.bot, 0))
            read.assert_not_called()

    def test_autonomy_uses_effective_relationship_and_can_still_choose_to_help(self):
        self.relation(age=90 * 86400)
        expected = relation_state(self.bot, 30)
        self.assertTrue(self.choose())
        payload = json.loads(self.bot.pool.chat.call_args.args[2])
        self.assertEqual(expected, payload['关系'])
        self.assertIn('有距离', payload['关系含义与边界'])
        self.assertIn('明显戒备', payload['关系含义与边界'])
        self.assertIn('认真具体的求助仍可认真处理', payload['关系含义与边界'])
        self.assertNotIn('仅限私聊的关系原因', str(self.bot.pool.chat.call_args))

    def test_quote_and_same_name_do_not_replace_actual_authors_relation(self):
        self.relation()
        self.relation(uid=31, favor=90, trust=90)
        quote = {'who_qq': 31, 'who_name': '同名群友', 'text': '引用别人的原话'}
        with patch.object(self.store, 'get_person_relation_effective',
                          wraps=self.store.get_person_relation_effective) as read:
            self.assertTrue(self.choose(quote=quote))
            self.assertEqual([(30, 90, 180)], [call.args for call in read.call_args_list])
        payload = json.loads(self.bot.pool.chat.call_args.args[2])
        self.assertIn('明显疏远', payload['关系含义与边界'])
        self.assertNotIn('亲近意愿较高', payload['关系含义与边界'])

    def test_low_relationship_can_inform_quiet_without_becoming_a_score_gate(self):
        self.relation()
        self.bot.pool.chat.return_value.text = '{"action":"quiet","mode":"quiet","minutes":1,"reason":"boundary"}'
        self.assertFalse(self.choose())
        self.assertEqual(1, self.bot.pool.chat.call_count)
        self.assertEqual('quiet', reply_autonomy.snapshot(self.store, self.bot.cfg)['last_action'])

    def test_administrator_permissions_do_not_replace_relationship_feelings(self):
        self.relation(uid=20)
        self.assertTrue(self.choose(uid=20))
        payload = json.loads(self.bot.pool.chat.call_args.args[2])
        self.assertTrue(payload['是已确认的管理员'])
        self.assertIn('明显疏远', payload['关系含义与边界'])
        self.assertIn('管理员权限独立', payload['关系含义与边界'])


if __name__ == '__main__':
    unittest.main()
