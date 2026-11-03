"""Nightmare delivery uses saved, owner-scoped fiction and a claimed contact event."""
import datetime as dt
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.environ.get('BOT_TEST_PATH') or str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
from dreaming import DreamRunner, fingerprint, local_day
from store import Store
import private_contact as courtesy


class Contact:
    def __init__(self, bot, conv, *, kind, event_key, material):
        self.bot, self.conv, self.event_key = bot, conv, event_key
        self.revision = bot.revision
        self.allowed = bot.contact_allowed and not bot.store.get_setting('test-attempt:' + event_key)
        self.claimed = False
        bot.contacts.append(dict(kind=kind, event_key=event_key, material=material))

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def check(self):
        return (self.allowed and self.bot.contact_allowed and not self.bot.cfg.get('bot_paused')
                and self.bot.revision == self.revision)
    def sending(self):
        if not self.check(): return False
        self.bot.store.set_setting('test-attempt:' + self.event_key, '1')
        self.claimed = True
        return True
    def sent(self): self.bot.confirmed.append(self.event_key)


class DreamPrivateNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.store = Store(os.path.join(self.tmp.name, 'bot.db'))
        self.now = int(dt.datetime(2026, 11, 2, 4, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
        self.clock = patch('dreaming.time.time', return_value=self.now)
        self.clock.start()
        self.contact_module = patch.dict(sys.modules, {'private_contact': SimpleNamespace(attempt=Contact)})
        self.contact_module.start()
        self.bot = SimpleNamespace(store=self.store, cfg={'bot_name': '测试鲸', 'owner_qq': 1},
                                   q=queue.Queue(), self_id=9, revision=0, contact_allowed=True,
                                   contacts=[], confirmed=[], _send=Mock(return_value=True),
                                   persona=SimpleNamespace(identity='嘴硬的测试鲸', reload_if_changed=lambda: None),
                                   pool=SimpleNamespace(chat=Mock(side_effect=self.draft)))
        self.runner = DreamRunner(self.bot, os.path.join(self.tmp.name, 'dream.lock'))
        self.body = '我梦见自己掉进很深的海底，四周突然没有光。我用力往上游，却怎么也看不见水面，远处还有奇怪的声音。我被突然扑过来的黑影吓醒了，才发现原来刚才都是梦。'

    def tearDown(self):
        self.contact_module.stop()
        self.clock.stop()
        self.store.conn.close()
        self.tmp.cleanup()

    def draft(self, *args, **kwargs):
        return SimpleNamespace(ok=True, text=json.dumps({'distressing': True, 'message': '刚梦见自己在黑乎乎的海底找不到出口，吓了一跳。原来只是梦，跟你说一声。'}, ensure_ascii=False), tool_calls=[])

    def saved(self, body=None, conv='p:1', uid=1, created=None, mixed=False):
        stamp = self.now if created is None else created
        sources = []
        for i in range(2):
            scope = 'g:3' if mixed and i else conv
            mid = self.store.add_message(scope, uid, '小甲', 'in', '今天看了海底纪录片，里面的小鱼很可爱%d' % i, ts=stamp-2000)
            row = dict(self.store.conn.execute('SELECT id,ts,conv,sender_id,sender_name,text FROM messages WHERE id=?', (mid,)).fetchone())
            row['hash'] = fingerprint(row)
            sources.append(row)
        generation = self.store.dream_settings()['generation']
        run = self.store.claim_dream(local_day(stamp), conv, sources, generation, stamp)
        self.store.dream_progress(run, 'dream', calls=2)
        self.assertTrue(self.store.finish_dream(run, sources, [], body or self.body, generation, stamp))
        return run, sources

    def test_saved_nightmare_sends_once_and_is_marked_fiction(self):
        run, _ = self.saved()
        self.assertTrue(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_called_once()
        self.assertEqual(['dream:%d' % run], self.bot.confirmed)
        row = self.store.conn.execute("SELECT id,attachments FROM messages WHERE direction='out'").fetchone()
        self.assertEqual('dream', json.loads(row['attachments'])[0]['type'])
        self.assertTrue(self.store.sleep_tainted(row['id']))
        restarted = DreamRunner(self.bot, os.path.join(self.tmp.name, 'dream.lock'))
        self.assertFalse(restarted._notify_nightmares(self.now))
        self.assertEqual(1, self.bot.pool.chat.call_count)

    def test_restart_scan_can_deliver_fresh_saved_dream_outside_generation_window(self):
        self.saved()
        daytime = self.now + 4*3600
        with patch('dreaming.time.time', return_value=daytime):
            self.assertEqual('waiting', self.runner.tick(daytime))
        self.bot._send.assert_called_once()

    def test_newly_completed_nightmare_checks_notification_after_save(self):
        for i in range(2):
            self.store.add_message('p:1', 1, '小甲', 'in', '今天看了海底纪录片，里面的小鱼很可爱%d' % i, ts=self.now-2000)
        def generate(system, prompt, temperature):
            if temperature < 0.5:
                data = json.loads(prompt.split('\n', 1)[1])
                return json.dumps({'moments': [{'source_id': r['id'], 'quote': r['text'], 'theme': '日常'} for r in data['sources']],
                                   'consolidations': []}, ensure_ascii=False), 'fake'
            return self.body, 'fake'
        self.runner.generate = generate
        self.runner.last_activity = self.now-3600
        self.assertEqual('complete', self.runner.tick(self.now))
        self.assertEqual(1, self.store.conn.execute('SELECT COUNT(*) FROM dream_entries').fetchone()[0])
        self.bot._send.assert_called_once()

    def test_ordinary_dream_uses_no_notification_model(self):
        self.saved('我梦见在云上吃饭，小鱼带着饭团飞来飞去。我一点也不害怕，只觉得很好笑。')
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot.pool.chat.assert_not_called()
        self.bot._send.assert_not_called()

    def test_model_must_confirm_narrators_own_distress(self):
        self.saved('我梦见小鱼做了噩梦，醒来找我聊天。我听完只觉得这鱼挺可爱。')
        self.bot.pool.chat.side_effect = lambda *a, **k: SimpleNamespace(ok=True, text='{"distressing":false,"message":""}')
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_not_called()

    def test_group_and_other_private_dreams_never_leave_their_scope(self):
        for conv, uid in (('g:2', 1), ('p:2', 2)):
            with self.subTest(conv=conv):
                self.store.conn.execute('DELETE FROM dream_entries')
                self.store.conn.execute('DELETE FROM dream_runs')
                self.store.conn.commit()
                self.saved(conv=conv, uid=uid)
                self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot.pool.chat.assert_not_called()

    def test_mixed_sources_do_not_leak_through_owner_scope(self):
        self.saved(mixed=True)
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot.pool.chat.assert_not_called()

    def test_expired_nightmare_does_not_become_a_backlog(self):
        self.saved(created=self.now-12*3600-1)
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot.pool.chat.assert_not_called()

    def test_disabled_or_blocked_contact_uses_no_model(self):
        self.saved()
        self.bot.contact_allowed = False
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot.pool.chat.assert_not_called()
        self.bot.contact_allowed = True
        self.store.save_dream_settings({'enabled': False, 'start': '03:00', 'end': '06:00', 'idle_minutes': 25})
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot.pool.chat.assert_not_called()

    def test_failed_or_unknown_delivery_is_not_retried(self):
        self.saved()
        self.bot._send.return_value = False
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_called_once()
        self.assertEqual([], self.bot.confirmed)

    def test_model_failure_is_not_retried(self):
        self.saved()
        self.bot.pool.chat.side_effect = RuntimeError('provider failed')
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.assertEqual(1, self.bot.pool.chat.call_count)
        self.bot._send.assert_not_called()

    def test_invalid_or_demanding_draft_is_not_sent(self):
        for output in ('not json', '{"distressing":true,"message":"我做噩梦了，你快来陪我好不好？"}',
                       '{"distressing":"true","message":"刚梦见自己落进黑漆漆的海底，吓了一跳。"}'):
            with self.subTest(output=output):
                self.store.set_setting('test-attempt:dream:1', '')
                if not self.store.conn.execute('SELECT 1 FROM dream_entries').fetchone(): self.saved()
                self.bot.pool.chat.side_effect = lambda *a, **k: SimpleNamespace(ok=True, text=output)
                self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_not_called()

    def test_source_or_dream_removed_during_drafting_cancels_delivery(self):
        run, sources = self.saved()
        def deleted(*args, **kwargs):
            result = self.draft(*args, **kwargs)
            self.store.conn.execute('DELETE FROM messages WHERE id=?', (sources[0]['id'],))
            self.store.conn.commit()
            return result
        self.bot.pool.chat.side_effect = deleted
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_not_called()

    def test_dream_deleted_during_drafting_cancels_delivery(self):
        run, _ = self.saved()
        def deleted(*args, **kwargs):
            result = self.draft(*args, **kwargs)
            self.store.delete_dream(run)
            return result
        self.bot.pool.chat.side_effect = deleted
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_not_called()

    def test_new_user_boundary_during_drafting_cancels_delivery(self):
        self.saved()
        def changed(*args, **kwargs):
            result = self.draft(*args, **kwargs)
            self.bot.revision += 1
            return result
        self.bot.pool.chat.side_effect = changed
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_not_called()

    def test_pause_during_drafting_cancels_delivery(self):
        self.saved()
        def paused(*args, **kwargs):
            result = self.draft(*args, **kwargs)
            self.bot.cfg['bot_paused'] = True
            return result
        self.bot.pool.chat.side_effect = paused
        self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_not_called()

    def test_real_policy_allows_one_nightmare_after_goodnight_and_blocks_other_streams(self):
        self.saved()
        with patch.dict(sys.modules, {'private_contact': courtesy}):
            courtesy.observe(self.bot, 'p:1', '晚安，我先睡觉了', ts=self.now-1800, message_id='goodnight')
            self.assertTrue(self.runner._notify_nightmares(self.now))
            self.assertFalse(self.runner._notify_nightmares(self.now))
            for kind in ('ordinary', 'video', 'share', 'tattle'):
                with courtesy.attempt(self.bot, 'p:1', kind=kind, event_key='another-' + kind, material='我被吓醒了') as contact:
                    self.assertFalse(contact.allowed, kind)
        self.bot._send.assert_called_once()
        self.assertEqual(1, self.bot.pool.chat.call_count)

    def test_real_policy_new_goodnight_during_drafting_cancels(self):
        self.saved()
        def changed(*args, **kwargs):
            result = self.draft(*args, **kwargs)
            courtesy.observe(self.bot, 'p:1', '晚安，我去睡了', ts=self.now, message_id='new-goodnight')
            return result
        self.bot.pool.chat.side_effect = changed
        with patch.dict(sys.modules, {'private_contact': courtesy}):
            self.assertFalse(self.runner._notify_nightmares(self.now))
            self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_not_called()
        self.assertEqual(1, self.bot.pool.chat.call_count)

    def test_real_policy_explicit_stop_during_drafting_cancels(self):
        self.saved()
        def changed(*args, **kwargs):
            result = self.draft(*args, **kwargs)
            courtesy.observe(self.bot, 'p:1', '不要再给我发消息了', ts=self.now, message_id='stop')
            return result
        self.bot.pool.chat.side_effect = changed
        with patch.dict(sys.modules, {'private_contact': courtesy}):
            self.assertFalse(self.runner._notify_nightmares(self.now))
            self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_not_called()
        self.assertEqual(1, self.bot.pool.chat.call_count)

    def test_real_policy_unknown_send_is_an_attempt_and_never_retried(self):
        self.saved()
        self.bot._send.return_value = False
        with patch.dict(sys.modules, {'private_contact': courtesy}):
            self.assertFalse(self.runner._notify_nightmares(self.now))
            self.assertFalse(self.runner._notify_nightmares(self.now))
        self.bot._send.assert_called_once()
        self.assertEqual(1, self.bot.pool.chat.call_count)


if __name__ == '__main__': unittest.main()
