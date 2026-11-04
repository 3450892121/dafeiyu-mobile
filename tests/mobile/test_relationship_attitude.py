"""Relationship meaning in actual reply context; all records are synthetic and local."""
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flow_memory import MemoryMixin
from store import Store
from main import Bot


class RelationshipAttitudeTests(unittest.TestCase):
    uid = 190009

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / 'bot.db'))
        self.addCleanup(self.store.conn.close)
        self.bot = MemoryMixin()
        self.bot.store = self.store
        self.bot.cfg = {'owner_qq': 190002, 'self_growth_enabled': True}
        self.bot.persona = SimpleNamespace(unified_self=False)
        now = int(time.time())
        self.bot._person_stats = Mock(return_value={
            'msg_count': 500, 'first_seen': now - 100 * 86400, 'last_seen': now})

    def relation(self, favor, trust):
        self.store.conn.execute(
            'INSERT INTO person_relations(qq,favor,trust,updated_at) VALUES(?,?,?,?) '
            'ON CONFLICT(qq) DO UPDATE SET favor=excluded.favor,trust=excluded.trust,'
            'updated_at=excluded.updated_at', (self.uid, favor, trust, int(time.time())))
        self.store.conn.commit()

    def block(self, is_group=False):
        return self.bot.build_relation_block(self.uid, '合成群友', is_group, 190003 if is_group else 0)

    def test_high_familiarity_does_not_cancel_distance_or_caution_in_any_mode(self):
        self.relation(4.1, 10)
        before = self.store.get_person_relation(self.uid)
        for growth, unified in ((True, False), (False, True), (True, True), (False, False)):
            with self.subTest(growth=growth, unified=unified):
                self.bot.cfg['self_growth_enabled'] = growth
                self.bot.persona.unified_self = unified
                block = self.block()
                self.assertIn('好感档位：冷淡；熟悉度：很熟；信任度：不太信', block)
                self.assertIn('明显疏远', block)
                self.assertIn('明显戒备', block)
                self.assertIn('认识很久不能抵消低好感或低信任', block)
                self.assertIn('对方先撒娇、叫亲昵称呼或道歉，也不等于你已经重新亲近', block)
                self.assertNotIn('不给每个档位规定口气', block)
                self.assertNotIn('故意冷落', block)
                self.assertNotIn('4.1', block)
        self.assertEqual(before, self.store.get_person_relation(self.uid))

    def test_high_favor_does_not_cancel_low_trust(self):
        self.relation(90, 10)
        block = self.block()
        self.assertIn('好感档位：很亲近', block)
        self.assertIn('亲近意愿较高', block)
        self.assertIn('明显戒备', block)

    def test_high_trust_does_not_cancel_low_favor(self):
        self.relation(4.1, 90)
        block = self.block()
        self.assertIn('信任度：很信任', block)
        self.assertIn('明显疏远', block)
        self.assertIn('比较信任', block)

    def test_fractional_scores_are_not_rounded_up_across_behavior_boundaries(self):
        for favor, trust, favor_tier, trust_tier in (
                (39.6, 29.6, '有点距离', '有点提防'),
                (19.6, 14.6, '冷淡', '不太信')):
            with self.subTest(favor=favor, trust=trust):
                self.relation(favor, trust)
                block = self.block()
                self.assertIn('好感档位：' + favor_tier, block)
                self.assertIn('信任度：' + trust_tier, block)

    def test_real_zero_and_missing_relation_have_different_meaning(self):
        self.relation(0, 0)
        block = self.block()
        self.assertIn('明显疏远', block)
        self.assertIn('明显戒备', block)
        unknown = self.bot.build_relation_block(190010, '合成新朋友', False, 0)
        self.assertIn('好感档位：正常', unknown)
        self.assertIn('信任度：普通', unknown)
        self.assertNotIn('明显疏远', unknown)
        self.assertNotIn('明显戒备', unknown)

    def test_confirmed_admin_authority_does_not_replace_emotional_relation(self):
        self.bot.cfg['owner_qq'] = self.uid
        self.relation(4.1, 10)
        block = self.block()
        self.assertIn('信任度：不太信', block)
        self.assertIn('明显疏远', block)
        self.assertIn('管理员权限独立按已确认账号和实际授权办理', block)
        self.assertNotIn('信任度：管理员', block)

    def test_help_and_honest_task_results_remain_compatible_with_distance(self):
        self.relation(4.1, 10)
        block = self.block()
        self.assertIn('认真具体的求助仍可认真处理', block)
        self.assertIn('帮助不代表恢复亲密或信任', block)
        self.assertIn('不用沉默惩罚，不要求讨好换回复', block)
        self.assertIn('不要求每句冷淡，不套固定话术', block)

    def test_group_context_keeps_distance_without_disclosing_private_events(self):
        self.relation(4.1, 10)
        secret = '合成私事：薄荷色信封里是未公开病历'
        self.store.apply_interaction(self.uid, 'p:%d' % self.uid, 'private1', secret,
            {'kind': 'neutral', 'target': 'self', 'confidence': .99, 'evidence': secret,
             'topic': '合成病历私事', 'state': 'active'})
        self.store.add_grudge(self.uid, secret)
        self.assertIn('薄荷色信封', self.block())
        group = self.block(is_group=True)
        self.assertIn('明显疏远', group)
        self.assertIn('以前相处有过不舒服，原因不要在群里透露', group)
        self.assertNotIn('薄荷色信封', group)
        self.assertNotIn('合成病历私事', group)
        self.assertIn('不把对一人的态度套给全群', group)

    def test_actual_reply_model_receives_distance_after_all_later_prompt_additions(self):
        self.relation(4.1, 10)
        bot = Bot.__new__(Bot)
        bot.store = self.store
        bot.cfg = dict(self.bot.cfg, reply_autonomy_enabled=False, bot_name='纳西妲',
                       trigger_name='小妲', web_search_enabled=False)
        bot.self_id = 190001
        bot.persona = SimpleNamespace(unified_self=False,
            system_prompt=Mock(return_value='合成当前人设。'),
            relation_context=Mock(return_value='管理员权限以当前账号和授权为准。'))
        bot.proactive_due, bot.proactive_seen, bot.sticker_streak = {}, {}, {}
        bot._song_wait = {}
        bot._send = Mock()
        bot.pool = SimpleNamespace(chat=Mock(side_effect=ReachedReplyModel))
        bot._person_stats = self.bot._person_stats
        bot._file_context = Mock(return_value=('', '', ''))
        bot._context = Mock(return_value=([], '', False))
        bot._song_pick_get = Mock(return_value=None)
        bot._music_intent = Mock(return_value=None)
        for method in ('_mark_group_at', '_proactive_clear_waiting', '_remember_sender',
                       '_note_latency', '_note_memory_candidate'):
            setattr(bot, method, Mock())
        for method in ('_sticker_bad_hit', '_sticker_good_hit', '_song_pick_hit'):
            setattr(bot, method, Mock(return_value=False))
        for method in ('_direct_image_reason', '_mentions_note', '_vb_context_block',
                       '_vb_growth_block', '_intent_context', 'intent_read_note',
                       '_share_awareness', '_token_block', '_maybe_relay', '_maybe_fill_reminder'):
            setattr(bot, method, Mock(return_value=''))
        bot._tool_defs = Mock(return_value=[])
        with patch('flow_reply.embed_texts', return_value=[]), \
             patch('flow_reply.voice.handle_command', return_value=False), \
             patch('flow_reply.cover.handle_request', return_value=False), \
             patch('flow_reply.voice.reply_mode', return_value='text'), \
             patch('interaction_state.observe'), \
             patch('recent_topic.schedule'):
            with self.assertRaises(ReachedReplyModel):
                bot._reply('g:190003', self.uid, 190003, '合成群友', False, True,
                           '这个周末你有什么想法？', [], {'message_id': 701}, at_kind='name')
        system = bot.pool.chat.call_args.args[0]
        self.assertIn('明显疏远', system)
        self.assertIn('明显戒备', system)
        self.assertIn('熟悉度：很熟', system)
        self.assertIn('认真具体的求助仍可认真处理', system)
        self.assertNotIn('不给每个档位规定口气', system)
        self.assertNotIn('4.1', system)
        bot._send.assert_not_called()


class ReachedReplyModel(Exception):
    pass


if __name__ == '__main__':
    unittest.main()
