"""Third-party relays and current voice boundaries use the same courtesy policy."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.environ.get('BOT_TEST_PATH') or str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
from flow_reply import ReplyMixin
from flow_media import MediaMixin, owner_note_event
from onebot import text_seg
from sleep_memory import local
from store import Store
import private_contact as courtesy
import tools_gate


class Bot(ReplyMixin, MediaMixin):
    pass


class ExtraPrivatePathsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.store = Store(os.path.join(self.tmp.name, 'bot.db'))
        self.now = int(dt.datetime(2026, 11, 2, 12, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
        self.clock = patch('private_contact.time.time', side_effect=lambda: self.now)
        self.clock.start()
        self.ready = patch.dict(tools_gate._READY, {'v': False})
        self.ready.start()
        tools_gate.ensure(self.store.conn)
        self.bot = Bot.__new__(Bot)
        self.bot.cfg = dict(owner_qq=1, bot_name='测试鲸', relay_enabled=True, relay_quiet_enabled=False,
                            relay_max_per_day=3, reply_autonomy_enabled=False)
        self.bot.store, self.bot.self_id = self.store, 9
        self.bot._proactive_now = lambda: self.now
        self.bot._is_proactive_quiet = lambda *args: False
        self.bot._send = Mock(return_value=True)
        self.bot._outbound_keys = lambda: []
        self.bot._text_segs = lambda s: [text_seg(s)]
        self.bot._person_name = lambda uid, name='': name
        self.bot._errand_recent = lambda *args: ''
        self.bot._note_is_harassment = Mock(return_value=False)
        self.store.get_person_relation = Mock(return_value={'trust': 60})
        self.bot.pool = SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True, text=json.dumps({
            'act': 'relay', 'urgent': True, 'say': '小甲让我跟你说，周五下午一起去取快递。'}))))
        self.bot._json_from_text = json.loads
        self.previous_reply = getattr(local, 'reply', None)
        local.reply = None

    def tearDown(self):
        local.reply = self.previous_reply
        self.ready.stop()
        self.clock.stop()
        self.store.conn.close()
        self.tmp.cleanup()

    def relay(self):
        return self.bot._maybe_relay('帮我转告你主人，周五下午一起去取快递', 2, '小甲', True, 'g:3')

    def note(self, is_owner=False, body='周五下午一起去取快递'):
        return self.bot._exec_extra_tool('note_to_owner', {'text': body}, 'p:1' if is_owner else 'g:3',
                                         not is_owner, 0 if is_owner else 3, 1 if is_owner else 2,
                                         is_owner, '主人' if is_owner else '小甲')

    def state(self, conv='p:1'):
        return json.loads(self.store.get_setting(courtesy.PREFIX + conv) or '{}')

    def test_relay_after_goodnight_is_not_drafted_or_sent(self):
        courtesy.observe(self.bot, 'p:1', '晚安，我去睡了', ts=self.now-1800, message_id='night')
        self.assertIn('没有转达', self.relay())
        self.bot.pool.chat.assert_not_called()
        self.bot._send.assert_not_called()

    def test_urgent_from_a_third_party_cannot_override_relay_quiet(self):
        self.bot._relay_quiet = Mock(return_value=True)
        self.assertIn('先不转达', self.relay())
        self.bot.pool.chat.assert_called_once()
        self.bot._send.assert_not_called()

    def test_relay_counts_against_other_streams_and_unanswered_retry(self):
        self.assertIn('已经私聊主人', self.relay())
        with courtesy.attempt(self.bot, 'p:1', kind='video', event_key='bvid:new') as contact:
            self.assertFalse(contact.allowed)
        self.now += 3*86400
        self.assertIn('没有转达', self.relay())
        self.bot._send.assert_called_once()

    def test_relay_generation_cancelled_by_new_goodnight(self):
        def generated(*args, **kwargs):
            courtesy.observe(self.bot, 'p:1', '晚安，我先睡了', message_id='new-night')
            return SimpleNamespace(ok=True, text='{"act":"relay","urgent":true,"say":"小甲让我跟你说，周五下午一起去取快递。"}')
        self.bot.pool.chat.side_effect = generated
        self.assertIn('没有转达', self.relay())
        self.bot._send.assert_not_called()

    def test_unknown_relay_is_never_retried(self):
        self.bot._send.return_value = False
        self.assertIn('没有确认送达', self.relay())
        self.now += 3*86400
        self.assertIn('没有转达', self.relay())
        self.bot._send.assert_called_once()

    def test_public_note_kept_locally_but_does_not_disturb_sleep(self):
        courtesy.observe(self.bot, 'p:1', '晚安，我去睡了', ts=self.now-1800, message_id='night')
        self.assertIn('没有转告送达', self.note())
        self.assertEqual(1, self.store.conn.execute('SELECT COUNT(*) FROM owner_notes').fetchone()[0])
        self.bot._send.assert_not_called()

    def test_public_note_unknown_delivery_does_not_claim_success_or_retry(self):
        self.bot._send.return_value = False
        self.assertIn('没有确认送达', self.note())
        self.now += 3*86400
        self.assertIn('没有转告送达', self.note())
        self.bot._send.assert_called_once()

    def test_relay_and_note_tool_cannot_duplicate_one_incoming_request(self):
        local.reply = {'conv': 'g:3', 'source_mid': 'original-message'}
        self.bot._send.return_value = False
        self.relay()
        self.now += 3*86400
        self.assertIn('没有转告送达', self.note(body='模型换了个说法，约你周五下午取快递'))
        self.bot._send.assert_called_once()

    def test_owners_explicit_task_delivery_still_works(self):
        courtesy.observe(self.bot, 'p:1', '不要再主动给我发消息', message_id='stop')
        self.assertEqual('已经转告主人了。', self.note(is_owner=True))
        self.bot._send.assert_called_once()

    def test_usage_notice_obeys_shared_boundary(self):
        courtesy.observe(self.bot, 'p:1', '晚安，我去睡了', ts=self.now-1800, message_id='night')
        with patch('flow_media.tools_gate.remind_needed', return_value=(True, 200, 200)):
            self.bot._tool_owner_remind('web_search', 1)
        self.bot._send.assert_not_called()

    def test_usage_notice_is_counted_as_one_contact(self):
        with patch('flow_media.tools_gate.remind_needed', return_value=(True, 200, 200)):
            self.bot._tool_owner_remind('web_search', 1)
            self.bot._tool_owner_remind('make_document', 1)
        self.bot._send.assert_called_once()
        self.assertEqual('notice', self.state()['pending'][0]['kind'])

    def test_current_voice_adds_goodnight_for_same_message_without_new_receipt(self):
        courtesy.observe(self.bot, 'p:1', '', message_id='voice', ts=self.now)
        before = self.state()
        self.bot._observe_voice_boundary('p:1', 1, {'message_id': 'voice', 'time': self.now}, ['晚安，我去睡觉了'], False)
        after = self.state()
        self.assertEqual('sleep', after['boundary'])
        self.assertEqual(before['in_at'], after['in_at'])
        self.assertEqual(before['in_id'], after['in_id'])
        self.assertEqual(before['pending'], after['pending'])

    def test_voice_other_than_boundary_does_not_create_a_second_receipt(self):
        courtesy.observe(self.bot, 'p:1', '', message_id='voice', ts=self.now)
        before = self.state()
        self.bot._observe_voice_boundary('p:1', 1, {'message_id': 'voice', 'time': self.now}, ['今天的鱼很好吃'], False)
        self.assertEqual(before, self.state())

    def test_replayed_and_bot_voice_do_not_change_owner_boundary(self):
        self.bot.cfg['bot_peers_qq'] = '2'
        self.bot.cfg['other_bots'] = '另一只:3'
        for uid, extra in [(1, {'_catchup': True}), (1, {'_replay': True}), (1, {'replay': True}),
                           (1, {'_redelivery': True}), (2, {}), (3, {}), (9, {}), (1, {'is_bot': True})]:
            with self.subTest(uid=uid, extra=extra):
                event = dict(message_id='voice', time=self.now, **extra)
                self.bot._observe_voice_boundary('p:%d' % uid, uid, event, ['晚安，我去睡了'], False)
        self.assertEqual({}, self.state())

    def test_current_group_voice_can_announce_rest_without_reading_private_messages(self):
        with courtesy.attempt(self.bot, 'p:1', kind='video', event_key='bvid:sent') as contact:
            self.assertTrue(contact.sending())
            contact.sent()
        self.now += 1800
        before = self.state()
        self.bot._observe_voice_boundary('g:3', 1, {'message_id': 'voice-group', 'time': self.now}, ['我先睡了，晚安'], True)
        after = self.state()
        self.assertEqual('sleep', after['boundary'])
        self.assertEqual(before['in_at'], after['in_at'])
        self.assertEqual(before['pending'], after['pending'])


if __name__ == '__main__': unittest.main()
