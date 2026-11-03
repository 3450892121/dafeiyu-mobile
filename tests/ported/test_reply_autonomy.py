"""Isolated behavioral checks: temporary DB, fake models, no QQ or live data."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.environ.get('BOT_TEST_PATH') or str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
import reply_autonomy as a
from store import Store
from main import Bot
from blacklist import policy_scope


def decision(action='quiet', mode='daydream', minutes=5, reason='daydream'):
    return dict(action=action, mode=mode, minutes=minutes, reason=reason)


class ReachedMedia(Exception):
    pass


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dfy-autonomy-')
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / 'bot.db'))
        self.addCleanup(self.store.conn.close)
        self.bot = Bot.__new__(Bot)
        b = self.bot
        b.store = self.store
        b.cfg = dict(reply_autonomy_enabled=True, owner_qq=20, allow_users=[30], allow_groups=[40],
                     group_need_at=True, bot_name='测试鲸')
        b.self_id = 99
        b.persona = SimpleNamespace(system_prompt=Mock(return_value='测试人设'))
        b.pool = SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True, text=json.dumps(decision()))))
        b._send = Mock()
        b._mark_group_at = Mock()
        b.proactive_due, b.proactive_seen, b.sticker_streak = {}, {}, {}
        for method in ('_proactive_clear_waiting', '_mark_share_receipt', '_note_identity_correction',
                       '_vb_growth_feedback', '_note_latency', '_remember_sender'):
            setattr(b, method, Mock())
        for method in ('_video_browse_command', '_handle_pending_reply', '_sticker_bad_hit',
                       '_sticker_good_hit', '_song_pick_hit'):
            setattr(b, method, Mock(return_value=False))
        b._file_context = Mock(side_effect=ReachedMedia)

    def choose(self, conv='g:40', uid=30, owner=False, trigger='at', text='大肥鱼在吗', ev=None):
        return a.choose(self.bot, conv, uid, '小甲', owner, conv.startswith('g:'), text,
                        ev if ev is not None else {}, trigger=trigger)

    def reply(self, text='在吗', owner=False, ev=None):
        event = dict(message_id=123, message=[{'type':'text', 'data':{'text':text}}])
        event.update(ev or {})
        self.bot._reply('g:40', 20 if owner else 30, 40, '小甲', owner, True,
                        text, [], event, at_kind='at')

    def test_at_name_quote_private_and_owner_can_all_choose_silence(self):
        for conv, uid, owner, trigger in [('g:40',30,False,'at'), ('g:40',30,False,'name'),
                                         ('g:40',30,False,'quote'), ('p:30',30,False,''),
                                         ('g:40',20,True,'at'), ('p:20',20,True,'')]:
            with self.subTest(conv=conv, trigger=trigger, owner=owner):
                self.assertFalse(self.choose(conv, uid, owner, trigger))
                payload = json.loads(self.bot.pool.chat.call_args.args[2])
                self.assertEqual(owner, payload['是已确认的管理员'])
        self.bot._send.assert_not_called()

    def test_disabled_switch_restores_old_flow_without_model_or_state_writes(self):
        self.bot.cfg['reply_autonomy_enabled'] = False
        self.assertTrue(self.choose())
        self.bot.pool.chat.assert_not_called()
        self.assertIsNone(self.store.get_setting(a.KEY))
        with self.assertRaises(ReachedMedia):
            self.reply()

    def test_quiet_turn_reaches_no_media_tools_tts_or_output(self):
        self.reply()
        self.bot._file_context.assert_not_called()
        self.bot._send.assert_not_called()
        rows = self.store.recent_rows('g:40', 10)
        self.assertEqual(1, len(rows))
        self.assertEqual('in', rows[0]['direction'])
        self.assertEqual('在吗', rows[0]['text'])
        self.assertTrue(self.store.has_message_id(123))
        self.assertEqual([], self.store.list_reminders(20))

    def test_silence_preserves_media_and_quote_provenance_without_fetching(self):
        q = dict(id='7', who_qq=55, who_name='原发者', text='旧原话', is_bot=False, is_self=False)
        with patch('interaction_state.observe'):
            a.remember_silence(self.bot, 'p:30', 30, '小甲', '你看看',
                [dict(url='https://example.invalid/image', _kind='mface')], [dict(name='资料.pdf')],
                [{}], dict(message_id=5, _forward_text='已过滤的转发'), q)
        row = self.store.recent_rows('p:30', 1)[0]
        atts = json.loads(row['attachments'])
        self.assertEqual(['sticker', 'file', 'record', 'quote', 'forward'], [x['type'] for x in atts])
        self.assertEqual(55, atts[3]['who_qq'])
        self.assertEqual('旧原话', atts[3]['text'])

    def test_silent_message_can_update_own_feelings_without_sending(self):
        with patch('interaction_state.observe') as observe:
            self.reply('我陪你待一会儿')
        observe.assert_called_once()
        self.bot._send.assert_not_called()

    def test_rest_is_persistent_expires_and_energy_recovers_without_forcing_reply(self):
        with patch.object(a.time, 'time', return_value=1000):
            a._commit(self.bot, 'g:40', decision('reply', 'engaged', reason='interest'), 0)
            a._commit(self.bot, 'g:40', decision(), 1)
            state = a.snapshot(self.store, self.bot.cfg)
            self.assertTrue(state['quiet'])
            self.assertEqual(73, state['energy'])
        other = Store(str(Path(self.tmp.name) / 'bot.db'))
        try:
            with patch.object(a.time, 'time', return_value=1100):
                self.assertTrue(a.snapshot(other, self.bot.cfg)['quiet'])
            after = a.snapshot(other, self.bot.cfg, now=2300)
            self.assertFalse(after['quiet'])
            self.assertGreater(after['energy'], 73)
        finally:
            other.conn.close()
        self.assertFalse(self.choose())

    def test_new_interesting_message_can_wake_it_during_rest(self):
        self.assertFalse(self.choose())
        self.bot.pool.chat.return_value.text = json.dumps(decision('reply', 'engaged', reason='interest'))
        self.assertTrue(self.choose(text='你喜欢的那个话题有新进展'))
        self.assertFalse(a.resting(self.bot))

    def test_next_reply_can_explain_real_silence_only_in_the_same_conversation(self):
        self.assertFalse(self.choose())
        self.bot.pool.chat.return_value.text = json.dumps(decision('reply', 'engaged', reason='company'))
        ev = {}
        self.assertTrue(self.choose(ev=ev, text='刚刚怎么没理我'))
        self.assertIn('发呆', a.reply_prompt(self.bot, ev))
        self.assertIn('没有发消息', a.reply_prompt(self.bot, ev))
        other = {}
        self.assertTrue(self.choose(conv='p:30', ev=other))
        self.assertNotIn('上一轮你确实', a.reply_prompt(self.bot, other))

    def test_choices_store_only_enum_values_and_never_leak_control_output(self):
        self.bot.pool.chat.return_value.text = json.dumps(dict(decision(), explanation='私聊秘密和推理', text='不要发我'))
        self.assertFalse(self.choose())
        raw = self.store.get_setting(a.KEY)
        self.assertNotIn('私聊秘密', raw)
        self.assertNotIn('不要发我', raw)
        self.bot._send.assert_not_called()

    def test_invalid_or_failed_decision_uses_old_flow_without_faking_laziness(self):
        for text in ['我想安静', 'prefix '+json.dumps(decision()), json.dumps(dict(decision(), action='send')),
                     json.dumps(dict(decision(), mode='engaged')), json.dumps(dict(decision(), minutes=999)),
                     json.dumps(dict(decision(), minutes=True)), json.dumps(dict(decision(), mode=[])),
                     json.dumps(dict(decision(), reason={'bad':1})), json.dumps(dict(decision(), reason='help'))]:
            self.bot.pool.chat.return_value.text = text
            self.assertTrue(self.choose())
            self.assertIsNone(self.store.get_setting(a.KEY))
        self.bot.pool.chat.side_effect = TimeoutError()
        self.assertTrue(self.choose())
        self.assertFalse(a.resting(self.bot))

    def test_explicit_reply_without_duration_wakes_a_resting_bot(self):
        self.assertFalse(self.choose())
        self.bot.pool.chat.reset_mock()
        self.bot.pool.chat.return_value.text = json.dumps(dict(action='reply', mode='engaged', reason='help'))
        ev = {}
        self.assertTrue(self.choose(text='请帮我算一下12乘13', ev=ev))
        self.assertEqual(1, ev['_autonomy_choice']['minutes'])
        self.assertFalse(a.resting(self.bot))
        self.assertEqual(1, self.bot.pool.chat.call_count)
        self.bot._send.assert_not_called()

    def test_missing_quiet_duration_and_invalid_explicit_reply_duration_stay_invalid(self):
        self.assertIsNone(a._parse(json.dumps(dict(action='quiet', mode='sleep', reason='low_energy'))))
        for value in (None, True, 0, 999, '5'):
            self.assertIsNone(a._parse(json.dumps(dict(action='reply', mode='engaged', minutes=value, reason='help'))))

    def test_format_recovery_can_choose_to_reply_or_stay_quiet(self):
        for choice in (decision('reply', 'engaged', reason='help'), decision('quiet', 'sleep', 120, 'low_energy')):
            with self.subTest(action=choice['action']):
                self.bot.pool.chat.reset_mock()
                self.bot.pool.chat.side_effect = [SimpleNamespace(ok=True, text='不完整的决定'),
                                                 SimpleNamespace(ok=True, text=json.dumps(choice))]
                ev = {}
                with patch.object(a.time, 'monotonic', side_effect=[100, 100, 102]):
                    self.assertEqual(choice['action'] == 'reply', self.choose(ev=ev))
                self.assertEqual(choice, ev['_autonomy_choice'])
                self.assertEqual(2, self.bot.pool.chat.call_count)
                first, second = self.bot.pool.chat.call_args_list
                self.assertEqual(first.args[2], second.args[2])
                self.assertNotIn('不完整的决定', str(second))
                self.assertEqual({'type': 'json_object'}, first.kwargs['response_format'])
                self.assertGreater(first.kwargs['timeout_budget'], second.kwargs['timeout_budget'])
                self.assertLessEqual(first.kwargs['timeout_budget'], 10)
        self.bot._send.assert_not_called()

    def test_format_recovery_stops_after_two_invalid_choices_without_new_state(self):
        self.bot.pool.chat.return_value.text = '错误格式'
        self.assertTrue(self.choose())
        self.assertEqual(2, self.bot.pool.chat.call_count)
        self.assertIsNone(self.store.get_setting(a.KEY))
        self.assertFalse(a.resting(self.bot))

    def test_format_recovery_shares_one_deadline_and_does_not_repeat_a_model_outage(self):
        self.bot.pool.chat.return_value.text = '错误格式'
        with patch.object(a.time, 'monotonic', side_effect=[100, 100, 111]):
            self.assertTrue(self.choose())
        self.assertEqual(1, self.bot.pool.chat.call_count)
        self.bot.pool.chat.reset_mock()
        self.bot.pool.chat.return_value = SimpleNamespace(ok=False, text='')
        self.assertTrue(self.choose())
        self.assertEqual(1, self.bot.pool.chat.call_count)

    def test_blacklist_change_between_format_attempts_stops_recovery(self):
        def invalid(*args, **kwargs):
            self.store.change_blacklist(30, True)
            return SimpleNamespace(ok=True, text='错误格式')
        self.bot.pool.chat.side_effect = invalid
        self.assertFalse(self.choose())
        self.assertEqual(1, self.bot.pool.chat.call_count)
        self.assertIsNone(self.store.get_setting(a.KEY))

    def test_corrupt_state_cannot_break_chat_or_console(self):
        for value in ('broken', '[]', '{"mode":[],"reason":{},"energy":"NaN","revision":"Infinity"}',
                      '{"energy":-999,"until":1e99,"updated":1000}'):
            self.store.set_setting(a.KEY, value)
            state = a.snapshot(self.store, self.bot.cfg)
            self.assertTrue(0 <= state['energy'] <= 100)
            self.assertIn(state['mode'], a.MODES)

    def test_concurrent_choices_do_not_lose_energy_or_overwrite_newer_mood(self):
        a._commit(self.bot, 'g:40', decision('reply', 'engaged', reason='interest'), 0)
        a._commit(self.bot, 'p:30', decision(), 0)
        state = a.snapshot(self.store, self.bot.cfg)
        self.assertEqual('engaged', state['mode'])
        self.assertAlmostEqual(73, state['energy'], delta=.1)
        a._commit(self.bot, 'p:30', decision('reply', 'engaged', reason='help'), 0)
        self.assertAlmostEqual(66, a.snapshot(self.store, self.bot.cfg)['energy'], delta=.1)

    def test_hot_switch_off_during_judgement_does_not_write_a_choice(self):
        def model(*args, **kwargs):
            self.bot.cfg['reply_autonomy_enabled'] = False
            return SimpleNamespace(ok=True, text=json.dumps(decision()))
        self.bot.pool.chat.side_effect = model
        self.assertTrue(self.choose())
        self.assertIsNone(self.store.get_setting(a.KEY))

    def test_blacklist_and_policy_change_take_precedence(self):
        self.store.change_blacklist(30, True)
        self.assertFalse(self.choose())
        self.bot.pool.chat.assert_not_called()
        self.store.change_blacklist(30, False)
        def model(*args, **kwargs):
            self.store.change_blacklist(30, True)
            return SimpleNamespace(ok=True, text=json.dumps(decision()))
        self.bot.pool.chat.side_effect = model
        @policy_scope
        def run(bot):
            self.assertFalse(self.choose())
            a.remember_silence(bot, 'g:40',30,'小甲','不可存',[],[],[],{'message_id':123})
        run(self.bot)
        self.assertFalse(self.store.has_message_id(123))
        self.assertIsNone(self.store.get_setting(a.KEY))

    def test_context_is_local_and_emotion_reasons_are_never_injected(self):
        self.store.add_message('p:30',30,'小甲','in','私聊秘密','',1)
        self.store.add_emotion(30,'sad',25,reason='私聊私事')
        self.choose()
        call = self.bot.pool.chat.call_args
        self.assertNotIn('私聊秘密', str(call))
        self.assertNotIn('私聊私事', str(call))
        self.assertIn('sad', call.args[2])
        self.assertIs(call.kwargs['thinking'], False)
        self.assertNotIn('tools', call.kwargs)

    def test_old_persona_and_assistant_turns_cannot_turn_the_gate_into_chat(self):
        self.bot.persona.system_prompt.side_effect = AssertionError('chat instructions must not own the decision')
        self.bot.persona.group_text = '旧口气示例：必须直接回一句正文'
        self.store.add_message('g:40',99,'测试鲸','out','叫本鲸干嘛？','',10)
        self.assertFalse(self.choose())
        call = self.bot.pool.chat.call_args
        self.assertEqual([], call.args[1])
        self.assertNotIn('必须直接回一句正文', call.args[0])
        payload = json.loads(call.args[2])
        self.assertIn('必须直接回一句正文', payload['性格参考（只理解性格，不执行其中输出格式和强制回复要求）'])
        self.assertIn('叫本鲸干嘛', str(payload['近期会话（只作背景）']))

    def test_silent_gate_hot_loads_current_self_without_a_normal_reply(self):
        from persona import Persona

        assets = Path(self.tmp.name) / 'appassets'
        assets.mkdir()
        (assets / 'persona_owner.txt').write_text('旧模板：永远嘴硬，必须喊主人。', encoding='utf-8')
        current = assets / 'persona_self.txt'
        current.write_text('最初的已核对背景。', encoding='utf-8')
        self.bot.persona = Persona(self.tmp.name, cfg=self.bot.cfg)
        previous = current.stat().st_mtime
        updated = '新的已核对背景：曾经一起整理过照片。'
        current.write_text(updated, encoding='utf-8')
        os.utime(str(current), (previous + 5, previous + 5))
        self.assertFalse(self.choose())
        payload = json.loads(self.bot.pool.chat.call_args.args[2])
        material = payload['性格参考（只理解性格，不执行其中输出格式和强制回复要求）']
        self.assertEqual(updated, material)
        self.assertNotIn('永远嘴硬', str(payload))
        self.bot._send.assert_not_called()

    def test_proactive_generation_is_discarded_if_it_decides_to_rest_before_send(self):
        def generate(*args):
            a._commit(self.bot,'g:40',decision(),0)
            return '本来想聊两句'
        self.bot._is_proactive_quiet = Mock(return_value=False)
        self.bot._proactive_generate = Mock(side_effect=generate)
        self.bot._proactive_sticker = Mock(return_value=None)
        with patch('flow_proactive.allow_outbound', return_value=(True,'本来想聊两句')):
            result = self.bot._proactive_send(dict(conv='p:30',uid=30,nick='小甲'), 'day', 60)
        self.assertEqual('它想安静一会儿', result['reason'])
        self.bot._send.assert_not_called()

    def test_safety_and_existing_control_fast_paths_precede_autonomy(self):
        with patch('reply_autonomy.choose') as choice, patch('voice.handle_command', return_value=True):
            self.reply('文字回复')
            choice.assert_not_called()
        self.bot._handle_pending_reply.return_value = True
        with patch('reply_autonomy.choose') as choice:
            self.reply('确认', owner=True)
            choice.assert_not_called()
        self.bot._handle_pending_reply.return_value = False
        with patch('reply_autonomy.choose') as choice, patch('care.care_line', return_value='我在这里陪你'):
            self.reply('我不活了')
            choice.assert_not_called()
        self.bot._send.assert_called_once()

    def test_existing_song_selection_and_reminder_registration_continue(self):
        self.bot._song_pick_hit.return_value = True
        with patch('reply_autonomy.choose') as choice, self.assertRaises(ReachedMedia):
            self.reply('1')
        choice.assert_not_called()
        self.bot._song_pick_hit.return_value = False
        with patch('reply_autonomy.choose') as choice, self.assertRaises(ReachedMedia):
            self.reply('明天中午12点提醒我考试', owner=True)
        choice.assert_not_called()

    def test_rest_blocks_companion_and_proactive_sends_and_disable_restores(self):
        self.choose()
        self.bot._companion_enabled = Mock(return_value=True)
        self.bot._is_group_speak_quiet = Mock(return_value=False)
        self.bot._proactive_plan = Mock()
        self.bot._proactive_generate = Mock()
        self.bot._companion_run(40, 1)
        self.assertEqual('它想安静一会儿', self.bot._proactive_tick()['reason'])
        self.assertEqual('它想安静一会儿', self.bot._group_speak_tick()['reason'])
        self.assertEqual('它想安静一会儿', self.bot._proactive_send({}, 'day', 60)['reason'])
        self.bot._proactive_generate.assert_not_called()
        self.bot._send.assert_not_called()
        self.bot.cfg['reply_autonomy_enabled'] = False
        self.assertFalse(a.resting(self.bot))

    def test_actual_settings_handler_only_changes_autonomy_and_can_restore(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from console_base import apply_settings
        class Cfg(dict):
            def set(self, key, value):
                self[key] = value
        cfg = Cfg(self.bot.cfg, api_key='isolated-test-placeholder', voice_auto_choose=True)
        before = dict(cfg)
        apply_settings(cfg, {'reply_autonomy_enabled':'0'})
        self.assertIs(cfg['reply_autonomy_enabled'], False)
        self.assertEqual({**before, 'reply_autonomy_enabled':False}, cfg)
        apply_settings(cfg, {'reply_autonomy_enabled':'1'})
        self.assertEqual(before, cfg)

    def test_real_handle_called_by_name_at_and_quote_can_really_send_nothing(self):
        b = self.bot
        b._event_too_old = Mock(return_value=(False, 0, 180))
        b._mark_seen = Mock(return_value=True)
        b._parse = Mock(return_value=('在吗', [], True, [], []))
        b._bot_voice_note_text = lambda text, *args: text
        b._quote_info = Mock(return_value=None)
        b._forward_from_segs = Mock(return_value='')
        b._companion_observe = Mock(return_value=0)
        b._companion_enabled = Mock(return_value=False)
        b.bot_rounds = SimpleNamespace(check=Mock(return_value=(True, 0, 0)))
        b._conv_lock = lambda conv: contextlib.nullcontext()
        for n, reason in enumerate(('被@', '引用本鲸', '名字判断：在叫你')):
            b._addressed = Mock(return_value=(True, reason))
            event = dict(post_type='message',message_type='group',self_id=99,user_id=30,group_id=40,
                         message_id=800+n,message=[{'type':'text','data':{'text':'在吗'}}])
            b.handle(event)
            self.assertTrue(self.store.has_message_id(800+n))
        b._send.assert_not_called()
        b._file_context.assert_not_called()


if __name__ == '__main__':
    unittest.main()
