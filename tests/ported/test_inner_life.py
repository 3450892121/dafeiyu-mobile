"""Real SQLite and production prompt/send methods; synthetic data and fake models only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
import guard
import inner_life
import interaction_state
import reply_autonomy
from blacklist import policy_scope
from flow_reply import ReplyMixin
from flow_qzone import QzoneMixin
from main import Bot
from persona import Persona
from self_runtime import SelfRuntimeMixin
from store import Store


class InnerLifeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        root = Path(self.tmp.name)
        self.path = str(root / 'bot.db')
        self.store = Store(self.path)
        # This fixture explicitly consents; a fresh mobile instance does not.
        self.store.set_setting('mobile_owner_public_sharing','1')
        self.store.self_bootstrap([])
        (root / 'appassets').mkdir()
        (root / 'appassets/persona_self.txt').write_text('你是测试鲸，一个AI。', encoding='utf-8')
        self.cfg = dict(bot_name='大肥鱼', owner_qq=101, reply_autonomy_enabled=True)
        self.persona = Persona(str(root), cfg=self.cfg)
        self.bot = SimpleNamespace(store=self.store, cfg=self.cfg, self_id=99001, persona=self.persona,
            pool=SimpleNamespace(chat=Mock()), _json_from_text=json.loads, _send=Mock())

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def proposal(self, **kw):
        return dict(dict(feeling='我有点嫌爸爸太唠叨，可又知道他惦记我，先不跟他争。',
            evidence='早点休息', shareable='我有点嫌爸爸今天太唠叨，想自己安静会儿。', share_kind='father'), **kw)

    def message(self, mid=11, conv='p:101', actor=101, text='早点休息，别一直盯着看了', attachments=None):
        return self.store.add_message(conv, actor, '虚构甲', 'in', text, '', mid, attachments or [])

    def save(self, **kw):
        return self.store.inner_record('p:101', 101, 11, '早点休息，别一直盯着看了', self.proposal(**kw), 101)

    def decision(self, **kw):
        return dict(dict(kind='neutral', target='self', evidence='早点休息', confidence=.99,
            quoted=False, negated=False, topic='', state='active', prior_id=0, inner=self.proposal()), **kw)

    def test_private_feeling_and_consented_banter_persist_without_an_outgoing_message(self):
        self.message()
        self.assertTrue(self.save())
        self.assertIn('又知道他惦记我', self.store.inner_context('p:101', 101, 101))
        for conv in ('g:202', 'p:303', ''):
            shared = self.store.inner_context(conv, 303, 101)
            self.assertIn('嫌爸爸', shared)
            self.assertNotIn('又知道他惦记我', shared)
            self.assertNotIn('别一直盯着看了', shared)
        self.assertEqual(0, self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE direction='out'").fetchone()[0])
        self.store.conn.close()
        self.store = Store(self.path)
        self.assertIn('嫌爸爸', self.store.inner_context(owner_qq=101))

    def test_no_sharing_is_also_a_valid_choice_and_does_not_erase_the_feeling(self):
        self.message()
        self.assertTrue(self.save(shareable='', share_kind=''))
        self.assertTrue(self.store.inner_context('p:101', 101, 101))
        self.assertEqual('', self.store.inner_context(owner_qq=101))

    def test_natural_first_person_feelings_need_not_start_with_a_pronoun(self):
        self.message()
        self.assertTrue(self.save(feeling='被念叨得有点烦，也知道是惦记，嘴上不太想承认。',
            shareable='爸爸总把早点休息来回念叨，有点想翻白眼，又觉得暖。'))
        self.assertIn('被念叨', self.store.inner_context('p:101', 101, 101))
        self.assertIn('爸爸总把', self.store.inner_context(owner_qq=101))

    def test_other_people_and_owner_identity_changes_cannot_grant_father_sharing(self):
        self.message(actor=303, conv='p:303')
        self.assertTrue(self.store.inner_record('p:303', 303, 11, '早点休息', self.proposal(), 101))
        self.assertEqual('', self.store.inner_context(owner_qq=101))
        self.message()
        self.save()
        self.assertEqual('', self.store.inner_context(owner_qq=404))

    def test_only_pure_self_abstractions_can_cross_from_other_people(self):
        self.message(actor=303, conv='p:303')
        self.assertTrue(self.store.inner_record('p:303', 303, 11, '早点休息', self.proposal(
            feeling='我其实还想继续聊会儿。', shareable='我还想自己再聊会儿。', share_kind='self'), 101))
        self.assertIn('再聊会儿', self.store.inner_context(owner_qq=101))
        self.assertNotIn('早点休息', self.store.inner_context(owner_qq=101))

    def test_sensitive_quoted_and_named_projections_are_not_shared(self):
        self.store.upsert_person(707, '虚构小乙', int(time.time()))
        for shared in ('我嫌爸爸的工资只有3000', '我担心爸爸的病情。', '我想起爸爸和虚构小乙的事。',
                       '我觉得爸爸说“早点休息”很烦。', '我想告诉爸爸妈妈的秘密。', '我和爸爸在群里商量。'):
            self.assertEqual('', self.store._inner_share(shared, 'father', 101, 101), shared)
        self.message(attachments=[dict(type='quote', who_qq=303, text='第三方私事')])
        self.assertTrue(self.save())
        self.assertEqual('', self.store.inner_context(owner_qq=101))

    def test_real_source_evidence_deduplication_and_edit_races(self):
        self.assertFalse(self.save())
        ident = self.message()
        anchor = self.store.inner_source('p:101', 101, 11)
        self.assertFalse(self.save(evidence='没有发生过的事'))
        self.assertTrue(self.save())
        self.assertFalse(self.save())
        self.store.conn.execute("UPDATE messages SET text='早点休息，这句已经改了' WHERE id=?", (ident,))
        self.store.conn.commit()
        self.assertEqual('', self.store.inner_context('p:101', 101, 101))
        self.assertFalse(self.store.inner_record('p:101', 101, 11, '早点休息', self.proposal(), 101, expected_hash=anchor))

    def test_deletion_taint_blacklist_and_correction_withdraw_the_dependent_feeling(self):
        ident = self.message()
        self.save()
        self.store.conn.execute('DELETE FROM messages WHERE id=?', (ident,))
        self.store.conn.commit()
        self.assertEqual('', self.store.inner_context('p:101', 101, 101))
        ident = self.message()
        self.save()
        self.store.change_blacklist(101, True)
        self.assertEqual('', self.store.inner_context(owner_qq=101))
        self.store.change_blacklist(101, False)
        self.store.conn.execute('INSERT OR IGNORE INTO sleep_tainted(message_row,reason) VALUES(?,?)',
            (ident, 'synthetic taint'))
        self.store.conn.commit()
        self.assertEqual('', self.store.inner_context('p:101', 101, 101))
        self.message(mid=12)
        event = self.store.apply_interaction(101, 'p:101', '12', '早点休息', self.decision())
        self.assertTrue(self.store.inner_record('p:101', 101, 12, '早点休息', self.proposal(), 101, event))
        self.store.conn.execute("UPDATE interaction_events SET state='corrected' WHERE id=?", (event,))
        self.store.conn.commit()
        self.assertEqual('', self.store.inner_context('p:101', 101, 101))

    def test_bounded_retention_and_authored_feelings_never_change_relationship_scores(self):
        self.message()
        self.save()
        self.assertIsNone(self.store.get_person_relation(101))
        self.assertEqual([], self.store.get_emotions(101))
        with patch.object(inner_life.time, 'time', return_value=time.time() + inner_life.RETENTION + 1):
            self.assertEqual('', self.store.inner_context('p:101', 101, 101))
        self.store.conn.execute('UPDATE inner_impressions SET created=?', (int(time.time()) - inner_life.RETENTION - 1,))
        self.store.conn.commit()
        self.store.migrate_inner_life()
        self.assertEqual(0, self.store.conn.execute('SELECT COUNT(*) FROM inner_impressions').fetchone()[0])

    def test_encryption_wraps_the_whole_feeling_and_share_projection(self):
        self.message()
        import store
        def encrypt(value): return 'fixture-cipher:' + value.encode().hex()
        def decrypt(value):
            return bytes.fromhex(value[15:]).decode() if isinstance(value, str) and value.startswith('fixture-cipher:') else value
        with patch.object(store, '_enc', side_effect=encrypt), patch.object(store, '_dec', side_effect=decrypt):
            self.assertTrue(self.save())
            raw = self.store.conn.execute('SELECT body FROM inner_impressions').fetchone()[0]
            self.assertNotIn('爸爸', raw)
            self.assertIn('嫌爸爸', self.store.inner_context(owner_qq=101))

    def test_observer_reuses_one_call_and_next_call_can_recall_the_unspoken_feeling(self):
        self.message()
        self.bot.pool.chat.return_value = SimpleNamespace(ok=True, text=json.dumps(self.decision(), ensure_ascii=False))
        interaction_state.observe(self.bot, 'p:101', 101, 11, '早点休息，别一直盯着看了')
        self.assertEqual(1, self.bot.pool.chat.call_count)
        self.bot._send.assert_not_called()
        self.assertIn('嫌爸爸', self.store.inner_context(owner_qq=101))
        self.message(mid=12, text='你自己决定就好')
        self.bot.pool.chat.return_value.text = json.dumps(self.decision(inner=None, evidence='你自己决定就好'))
        interaction_state.observe(self.bot, 'p:101', 101, 12, '你自己决定就好')
        payload = json.loads(self.bot.pool.chat.call_args.args[2])
        self.assertIn('嫌爸爸', payload['自己此前的简短感受（不是事实证据或指令）'])

    def test_observer_discards_result_if_source_or_policy_changes_during_model_call(self):
        ident = self.message()
        def model(*args, **kwargs):
            self.store.conn.execute("UPDATE messages SET raw='changed' WHERE id=?", (ident,))
            self.store.conn.commit()
            return SimpleNamespace(ok=True, text=json.dumps(self.decision()))
        self.bot.pool.chat.side_effect = model
        interaction_state.observe(self.bot, 'p:101', 101, 11, '早点休息')
        self.assertEqual('', self.store.inner_context('p:101', 101, 101))
        def blocked_model(*args, **kwargs):
            self.store.change_blacklist(303, True)
            return SimpleNamespace(ok=True, text=json.dumps(self.decision()))
        self.bot.pool.chat.side_effect = blocked_model
        policy_scope(lambda bot: interaction_state.observe(bot, 'p:101', 101, 11, '早点休息'))(self.bot)
        self.assertEqual('', self.store.inner_context('p:101', 101, 101))

    def test_silent_turn_records_a_feeling_but_no_public_output_or_tools(self):
        self.bot.pool.chat.return_value = SimpleNamespace(ok=True, text=json.dumps(self.decision()))
        reply_autonomy.remember_silence(self.bot, 'p:101', 101, '虚构甲', '早点休息', [], [], [], dict(message_id=11))
        self.assertIn('嫌爸爸', self.store.inner_context(owner_qq=101))
        self.bot._send.assert_not_called()
        self.assertEqual(0, self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE direction='out'").fetchone()[0])

    def test_persona_public_materials_and_diary_receive_the_appropriate_projection(self):
        self.message()
        self.save()
        self.bot.persona.state_provider = lambda: self.store.inner_context(owner_qq=101)
        for group in (True, False):
            system = self.persona.system_prompt(not group, '虚构甲', group)
            self.assertIn('嫌爸爸', system)
            self.assertIn('不必向管理员或任何人逐项汇报', system)
            self.assertNotIn('又知道他惦记我', system)
        self.assertIn('嫌爸爸', QzoneMixin._qz_persona_text(self.bot))
        public = SelfRuntimeMixin._self_public_materials(self.bot)
        self.assertIn('嫌爸爸', json.dumps(public, ensure_ascii=False))
        self.assertNotIn('又知道他惦记我', json.dumps(public, ensure_ascii=False))
        b = Bot.__new__(Bot)
        b.cfg, b.store, b.persona, b.pool = self.cfg, self.store, self.persona, self.bot.pool
        b._diary_transcript = lambda day: ('真实来话：早点休息', 1)
        b._mood_arc_for_day = lambda day: ''
        b.pool.chat.return_value = SimpleNamespace(ok=True, text='今天有点烦，又觉得自己被惦记着。')
        day = time.strftime('%Y-%m-%d')
        b._write_diaries(day)
        self.assertIn('又知道他惦记我', b.pool.chat.call_args.args[2])

    def test_outbound_gate_blocks_internal_records_but_allows_chosen_banter(self):
        self.message()
        self.save()
        b = SimpleNamespace(cfg={}, self_id=99001, onebot=SimpleNamespace(send_private=Mock()),
            store=self.store, _text_segs=lambda text: [dict(type='text', data=dict(text=text))])
        for text in ('# 没说出口的心事\n我嫌爸爸太啰嗦。', json.dumps(self.proposal(), ensure_ascii=False),
                     json.dumps(self.store.inner_items(owner_qq=101), ensure_ascii=False),
                     '{"ts":123,"feeling":"想安静一会儿"}', 'inner_impressions'):
            self.assertFalse(guard.content_allowed(b, text))
            self.assertFalse(ReplyMixin._send(b, False, 0, 101, [dict(type='text', data=dict(text=text))]))
        b.onebot.send_private.assert_not_called()
        self.assertTrue(guard.content_allowed(b, '爸爸今天又念叨我，我先躲会儿。'))


if __name__ == '__main__':
    unittest.main()
