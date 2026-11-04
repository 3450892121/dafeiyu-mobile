"""Character-aware helper prompts and send-time mention controls; transports stay mocked."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from main import Bot
from onebot import at_seg, text_seg
from flow_reply import _qzone_cmd


class CharacterReplyPromptTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot.__new__(Bot)
        self.bot.cfg = {'bot_name': '纳西妲', 'trigger_name': '小妲', 'owner_qq': 190002}
        self.bot.self_id = 190001
        self.bot.store = Mock()
        self.bot.store.is_blocked.return_value = False
        self.bot.store.get_person_relation.return_value = {'trust': 50}
        self.bot.store.self_context.return_value = ''
        self.bot._errand_recent = Mock(return_value='群友说：大肥鱼是另外一个账号。')
        self.bot.pool = Mock()
        self.bot.pool.chat.return_value = SimpleNamespace(ok=True, text='{"act":"none"}')

    def test_relay_and_errand_judgements_keep_current_identity_and_real_other_names(self):
        contact = Mock()
        contact.check.return_value = True
        with patch('flow_reply.private_contact.attempt', return_value=contact):
            self.bot._maybe_relay('请告诉管理员，大肥鱼今天来过。', 190009, '合成群友', True, 'g:190003')
        system, _, prompt = self.bot.pool.chat.call_args.args
        self.assertIn('「纳西妲」', system)
        self.assertIn('「纳西妲」', prompt)
        self.assertIn('大肥鱼今天来过。', prompt)
        self.assertNotIn('你是蓝色大肥鱼', system + prompt)
        self.bot._errand_candidates = Mock(return_value=[{'qq': 190009, 'name': '合成群友'}])
        self.bot._maybe_errand('告诉合成群友，大肥鱼是另外一个机器人。', 'p:190002')
        system, _, prompt = self.bot.pool.chat.call_args.args
        self.assertIn('「纳西妲」', system)
        self.assertIn('大肥鱼是另外一个机器人。', prompt)
        self.assertNotIn('你是蓝色大肥鱼', system + prompt)

    def test_errand_rewrite_does_not_reset_character(self):
        self.bot._errand_candidates = Mock(return_value=[{'qq': 190009, 'name': '合成群友'}])
        self.bot._outbound_keys = Mock(return_value=[])
        self.bot._text_segs = lambda text: [text_seg(text)]
        self.bot._send = Mock(return_value=True)
        self.bot.pool.chat.side_effect = [
            SimpleNamespace(ok=True, text=json.dumps({'act': 'send', 'target_qq': 190009,
                                                      'say': '明天见，这句话提到了主人。'})),
            SimpleNamespace(ok=True, text='明天见，到时我们再聊。')]
        with patch('flow_reply.random.random', return_value=1.0), \
             patch('flow_reply.allow_outbound', side_effect=lambda text, *args, **kwargs: (True, text)), \
             patch('flow_reply.style_clean', side_effect=lambda text, **kwargs: text):
            self.bot._maybe_errand('告诉合成群友明天见。', 'p:190002')
        self.assertEqual(self.bot.pool.chat.call_count, 2)
        rewrite = self.bot.pool.chat.call_args.args[0]
        self.assertIn('「纳西妲」', rewrite)
        self.assertNotIn('大肥鱼', rewrite)
        self.bot._send.assert_called_once()

    def test_qzone_command_and_self_image_only_use_current_names(self):
        names = self.bot._bot_names()
        for name in names:
            with self.subTest(name=name):
                topic, _ = _qzone_cmd(name + '发个QQ空间介绍新角色', names)
                self.assertEqual(topic, '介绍新角色')
                self.assertTrue(self.bot._draw_subject_is_self(name + '的画像'))
        for name in ('大肥鱼', '蓝色大肥鱼', '本鲸', '鲸', '肥鱼'):
            with self.subTest(old=name):
                self.assertIsNone(_qzone_cmd(name + '发个空间介绍自己', names)[0])
                self.assertFalse(self.bot._draw_subject_is_self(name + '的画像'))
        self.assertIsNone(_qzone_cmd('小妲不要发个空间', names)[0])

    def test_search_cleaning_keeps_other_character_names_as_subjects(self):
        self.assertEqual(self.bot._clean_search_query('小妲 纳西妲 今天新闻'), '今天新闻')
        self.assertEqual(self.bot._clean_search_query('大肥鱼 最近消息'), '大肥鱼 最近消息')


class GroupMentionControlTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot.__new__(Bot)
        self.bot.self_id = 190001
        self.bot.cfg = {'group_at_reply': True, 'group_at_reply_at': True,
                        'group_at_reply_name': True, 'group_at_reply_quote': True}
        self.bot.store = Mock()
        self.bot.store.is_blocked.return_value = False
        self.bot.onebot = Mock()
        self.bot.onebot.send_group.return_value = {'message_id': 701}
        self.bot._text_segs = lambda text: [text_seg(text)]

    def send(self, text='这是合成回复。', explicit=()):
        with patch('flow_reply.guard.content_allowed', return_value=True), \
             patch('flow_reply.note_delivery'):
            self.assertTrue(self.bot._send(True, 190003, 190002, [*explicit, text_seg(text)]))
        return self.bot.onebot.send_group.call_args.args[1]

    def test_switch_off_while_composing_prevents_cached_automatic_mention(self):
        self.assertTrue(self.bot._mark_group_at(True, 190003, 190002, 'at'))
        self.bot.cfg['group_at_reply'] = False
        self.assertEqual(self.send(), [text_seg('这是合成回复。')])

    def test_trigger_subswitches_are_rechecked_without_changing_other_modes(self):
        self.bot._mark_group_at(True, 190003, 190002, 'name')
        self.bot.cfg['group_at_reply_name'] = False
        self.assertEqual(self.send(), [text_seg('这是合成回复。')])
        self.bot._mark_group_at(True, 190003, 190002, 'at')
        self.assertEqual(self.send()[0], at_seg(190002))

    def test_enabled_reply_mentions_once_and_disabled_reply_preserves_actual_words(self):
        self.bot._mark_group_at(True, 190003, 190002, 'quote')
        self.assertEqual(self.send()[0], at_seg(190002))
        self.assertEqual(self.send(), [text_seg('这是合成回复。')])
        self.bot.cfg['group_at_reply'] = False
        self.bot._mark_group_at(True, 190003, 190002, 'at')
        text = '大肥鱼是另一个机器人，正文里的 @群友 保留。'
        self.assertEqual(self.send(text), [text_seg(text)])

    def test_disabling_automatic_mentions_does_not_erase_an_explicit_message_segment(self):
        self.bot.cfg['group_at_reply'] = False
        self.bot._mark_group_at(True, 190003, 190002, 'at')
        self.assertEqual(self.send(explicit=[at_seg(190009)])[0], at_seg(190009))


if __name__ == '__main__':
    unittest.main()
