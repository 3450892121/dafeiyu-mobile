"""Current-character names alone drive name gates; all QQ/model operations are mocks."""
import contextlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from common import bot_identity_note, group_at_kind, message_targets_bot
from identity import Identity
from main import Bot
from flow_proactive import ProactiveMixin


OLD_NAMES = ('大肥鱼', '蓝色大肥鱼', '本鲸', '鲸', '肥鱼', '鲸鱼娘')


class CharacterWakeWordTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot.__new__(Bot)
        self.bot.cfg = {'bot_name': '纳西妲', 'trigger_name': '小妲',
                        'owner_qq': 190002, 'allow_groups': [190003],
                        'group_need_at': True, 'group_trigger_name': True,
                        'group_trigger_quote': True}
        self.bot.self_id = 190001
        self.bot.store = Mock()
        self.bot.store.is_blocked.return_value = False
        self.bot.store.has_message_id.return_value = False
        self.bot.store.same_name_people.return_value = []
        self.bot.store.messages_for_context.return_value = []
        self.bot.identity = Identity(self.bot.store, self_id=190001, cfg=self.bot.cfg)
        self.bot._event_too_old = Mock(return_value=(False, 0, 1800))
        self.bot._mark_seen = Mock(return_value=True)
        self.bot._quote_info = Mock(return_value=None)
        self.bot._companion_observe = Mock(return_value=0)
        self.bot._companion_enabled = Mock(return_value=False)
        self.bot._companion_state = Mock()
        self.bot._sticker_bad_hit = Mock(return_value=False)
        self.bot._song_pick_hit = Mock(return_value=False)
        self.bot._store_passive = Mock()
        self.bot._judge_name_mention = Mock(return_value=(True, '叫到当前实例'))
        self.bot.bot_rounds = SimpleNamespace(check=Mock(return_value=(True, 0, 0)))
        self.bot._conv_lock = lambda conv: contextlib.nullcontext()
        self.bot._note_latency = Mock()
        self.bot._reply = Mock()

    def event(self, text, extra=()):
        return {'post_type': 'message', 'message_type': 'group', 'self_id': 190001,
                'user_id': 190002, 'group_id': 190003, 'message_id': 700,
                'sender': {'nickname': '合成管理员'},
                'message': [*extra, {'type': 'text', 'data': {'text': text}}]}

    def handle(self, text, extra=()):
        with patch('private_contact.observe'):
            self.bot.handle(self.event(text, extra))

    def test_current_trigger_reaches_real_group_gate_and_old_names_do_not(self):
        for old in OLD_NAMES:
            with self.subTest(old=old):
                self.handle(old + '在吗')
                self.assertFalse(self.bot._name_mentioned(old))
                self.assertFalse(self.bot._calls_bot(old + '在吗'))
        self.bot._judge_name_mention.assert_not_called()
        self.bot._reply.assert_not_called()
        self.handle('小妲在吗')
        self.bot._judge_name_mention.assert_called_once()
        self.bot._reply.assert_called_once()
        self.assertEqual(self.bot._bot_names(), ['纳西妲', '小妲'])

    def test_configured_identity_and_trigger_hot_change_without_old_fallback(self):
        self.bot.cfg.update(bot_name='测试角色', trigger_name='专用称呼')
        self.assertEqual(self.bot.identity.bot_name(), '测试角色')
        self.assertEqual(self.bot._bot_names(), ['测试角色', '专用称呼'])
        self.assertTrue(self.bot._name_mentioned('专 用 称 呼，帮我看看'))
        self.assertFalse(self.bot._name_mentioned('小妲'))
        self.bot.cfg.pop('trigger_name')
        self.assertEqual(self.bot._trigger_name(), '测试角色')
        self.bot.cfg.clear()
        self.assertEqual(self.bot.identity.bot_name(), '机器人')
        self.assertEqual(self.bot._bot_name(), '机器人')
        self.assertTrue(all(not self.bot._name_mentioned(old) for old in OLD_NAMES))

    def test_self_at_and_self_quote_keep_their_gates(self):
        self.handle('你好', [{'type': 'at', 'data': {'qq': '190001'}}])
        self.bot._reply.assert_called_once()
        self.bot._judge_name_mention.assert_not_called()
        self.bot._reply.reset_mock()
        self.bot._quote_info.return_value = {'is_self': True, 'text': '合成旧回复'}
        self.handle('继续说', [{'type': 'reply', 'data': {'id': '701'}}])
        self.bot._reply.assert_called_once()
        self.bot._judge_name_mention.assert_not_called()
        _, reason = self.bot._addressed([], '继续说', self.bot._quote_info.return_value)
        self.assertEqual(group_at_kind(reason), 'quote')

    def test_at_others_or_all_still_does_not_wake_current_character(self):
        for target in ('190009', 'all'):
            with self.subTest(target=target):
                self.handle('小妲在吗', [{'type': 'at', 'data': {'qq': target}}])
        self.bot._reply.assert_not_called()
        self.bot._judge_name_mention.assert_not_called()

    def test_name_judgement_examples_use_current_trigger_and_preserve_real_history(self):
        prompt = self.bot._name_judge_prompt('g:190003', 190002, '合成管理员', '小妲', [], None)
        self.assertIn('昵称「纳西妲」', prompt)
        self.assertIn('小妲在吗', prompt)
        self.assertTrue(all(old not in prompt for old in OLD_NAMES))
        self.bot.store.messages_for_context.return_value = [
            {'role': 'user', 'content': '[群友(QQ190009)] 大肥鱼是另一个机器人。'}]
        prompt = self.bot._name_judge_prompt('g:190003', 190002, '合成管理员', '小妲', [], None)
        self.assertIn('大肥鱼是另一个机器人。', prompt)

    def test_share_evidence_only_uses_passed_instance_names_plus_self_at_or_quote(self):
        names = self.bot._bot_names()
        for old in OLD_NAMES:
            with self.subTest(old=old):
                self.assertFalse(message_targets_bot({'direction': 'in', 'text': old + '在吗'},
                                                     190001, bot_names=names)[0])
        self.assertTrue(message_targets_bot({'direction': 'in', 'text': '小 妲在吗'},
                                            190001, bot_names=names)[0])
        self.assertFalse(message_targets_bot({'direction': 'in', 'text': '大肥鱼'}, 190001)[0])
        self.assertTrue(message_targets_bot({'direction': 'in', 'raw': json.dumps({
            'message': [{'type': 'at', 'data': {'qq': '190001'}}]})}, 190001)[0])
        self.assertTrue(message_targets_bot({'direction': 'in', 'attachments': json.dumps([
            {'type': 'quote', 'is_self': True}])}, 190001)[0])
        note = bot_identity_note(190001, '纳西妲')
        self.assertIn('「纳西妲」', note)
        self.assertTrue(all(old not in note for old in OLD_NAMES))
        self.assertNotIn('大肥鱼', bot_identity_note(190001))

    def test_companion_quiet_request_does_not_reuse_other_character_aliases(self):
        for old in OLD_NAMES:
            self.bot._companion_quiet_request(190003, old + '别插话', False)
        self.bot._companion_state.quiet.assert_not_called()
        self.bot._companion_quiet_request(190003, '小妲别插话', False)
        self.bot._companion_state.quiet.assert_called_once_with(190003)
        self.bot._companion_state.quiet.reset_mock()
        self.bot._companion_quiet_request(190003, '别插话', True)
        self.bot._companion_state.quiet.assert_called_once_with(190003)

    def test_proactive_persona_failure_retains_configured_name(self):
        bot = ProactiveMixin()
        bot.cfg = {'bot_name': '纳西妲'}
        bot.persona = Mock()
        bot.persona.system_prompt.side_effect = ValueError('synthetic missing persona')
        bot._proactive_personal_context = lambda system, *args, **kwargs: (system, [])
        bot._proactive_open_prompt = lambda *args: '合成主动联系依据'
        bot._proactive_recent_texts = lambda *args: []
        bot._proactive_text_seen = lambda *args: False
        bot.pool = Mock()
        bot.pool.chat.return_value = SimpleNamespace(ok=True, text='上次的植物话题，我想再研究一下。')
        self.assertTrue(bot._proactive_generate({'is_owner': True, 'conv': 'p:190002',
                                               'uid': 190002}, 30))
        self.assertIn('「纳西妲」', bot.pool.chat.call_args.args[0])
        self.assertNotIn('大肥鱼', bot.pool.chat.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
