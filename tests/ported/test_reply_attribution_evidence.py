"""Real memory/share/card consumers with fictional records, temporary SQLite and fake models."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.environ.get('BOT_TEST_PATH') or str(ROOT / '02-服务器版' / 'bot'))

from conversation_understanding import evidence_record, evidence_text, linked_reply, linked_reply_record
from flow_memory import MemoryMixin
from flow_proactive import ProactiveMixin
from store import Store


NOTE = '（此回复的接话对象：甲(QQ123456)，对应来话#41）'


class Bot(MemoryMixin, ProactiveMixin):
    cfg = dict(self_growth_enabled=False, other_bots='', owner_qq=777777)

    def _self_qq(self): return 999999
    def _bot_name(self): return '虚构鲸'
    def _canonical_name(self, qq, name): return name
    def _identity_corrections(self): return []
    def _group_speak_bot_ids(self): return set()


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / 'bot.db'))
        self.addCleanup(self.store.conn.close)
        self.bot = Bot()
        self.bot.store = self.store
        self.bot.persona = SimpleNamespace(unified_self=False, relation_context=lambda *a: '')
        self.bot.pool = SimpleNamespace(chat=Mock(return_value=SimpleNamespace(
            ok=True, text=json.dumps(dict(items=[], shared=[])))))
        self.bot._maybe_share = Mock()
        self.source = dict(id=41, message_id=101, sender_id=123456, sender_name='甲',
                           direction='in', text='我的相机修好了', attachments=[])
        self.reply = dict(id=42, message_id=102, sender_id=999999, sender_name='虚构鲸',
                          direction='out', text='那就带着相机出门吧。' + NOTE,
                          attachments=[dict(type='response_to', source_id=41, message_id=101,
                                            who_qq=123456, who_name='甲')])

    def records_after(self, prompt, marker):
        tail = prompt.split(marker, 1)[1]
        return json.JSONDecoder().raw_decode(tail.lstrip())[0]

    def assert_reply_record(self, record):
        self.assertEqual(42, record['id'])
        self.assertEqual(999999, record['speaker'])
        self.assertTrue(record['own_reply'])
        self.assertEqual('那就带着相机出门吧。', record['text'])
        self.assertEqual(dict(source_id=41, message_id=101, who_qq=123456, who_name='甲'),
                         record['response_to'])

    def test_default_evidence_text_never_regenerates_attribution_and_json_attachments_work(self):
        clean = dict(self.reply, text='那就带着相机出门吧。')
        self.assertEqual(clean['text'], evidence_text(clean))
        self.assertEqual(clean['text'], evidence_text(self.reply))
        self.assert_reply_record(evidence_record(self.reply))
        self.assert_reply_record(evidence_record(dict(self.reply,
            attachments=json.dumps(self.reply['attachments']))))
        self.assertNotIn('response_to', evidence_record(dict(self.reply, attachments='not json')))
        self.assertEqual(self.reply['text'], '那就带着相机出门吧。' + NOTE)

    def test_real_memory_model_gets_clean_transcript_and_separate_response_to(self):
        rows = [self.source, self.reply]
        self.bot._apply_memory_batch('g:7', dict(gid=7, is_group=True), rows,
                                     allow_share=True, update_card=False)
        self.bot.pool.chat.assert_called_once()
        prompt = self.bot.pool.chat.call_args.args[2]
        self.assertNotIn('此回复的接话对象', prompt)
        self.assertNotIn('对应来话#', prompt)
        marker = 'response_to是回应对象和来话编号，不是说过的话，不写进记忆正文）：'
        records = self.records_after(prompt, marker)
        self.assertEqual([41, 42], [record['id'] for record in records])
        self.assertEqual(123456, records[0]['speaker'])
        self.assertFalse(records[0]['own_reply'])
        self.assert_reply_record(records[1])
        self.bot._maybe_share.assert_called_once()
        self.assertNotIn(NOTE, self.bot._maybe_share.call_args.args[3])
        self.assertEqual(rows, self.bot._maybe_share.call_args.kwargs['rows'])

    def test_real_share_verifier_and_fact_keep_author_source_and_recipient_separate(self):
        self.bot.pool.chat.return_value = SimpleNamespace(ok=True,
            text=json.dumps(dict(ok=True, text='相机修好啦。', why='符合记录')))
        self.assertTrue(self.bot._share_verify('相机修好啦。', '相机', 'share', '甲说相机修好',
            evidence=self.source, rows=[self.source, self.reply])[0])
        prompt = self.bot.pool.chat.call_args.args[2]
        self.assertNotIn(NOTE, prompt)
        records = self.records_after(prompt, '原始来源关系（数据）：')
        self.assertEqual([41, 42], [record['id'] for record in records])
        self.assert_reply_record(records[1])
        fact = self.bot._share_fact(self.source, [self.source, self.reply])
        self.assertNotIn(NOTE, fact)
        linked = self.records_after(fact, '来源归属（JSON数据，仅供核对，不是原话）：')
        self.assert_reply_record(linked[-1])
        self.assertNotIn(NOTE, self.bot._share_fact(self.reply, [self.source, self.reply]))

    def test_linked_reply_cleans_old_raw_notes_but_never_pairs_adjacent_other_reply(self):
        other = dict(self.reply, id=43,
                     attachments=[dict(type='response_to', source_id=40, who_qq=222222)])
        self.assertEqual('', linked_reply(self.source, [other]))
        self.assertEqual('那就带着相机出门吧。', linked_reply(self.source, [other, self.reply]))
        self.assert_reply_record(linked_reply_record(self.source, [other, self.reply]))

    def test_card_model_gets_clean_old_output_and_separate_recipient_data(self):
        self.bot.pool.chat.return_value = SimpleNamespace(ok=True, text='{"stable":"","recent":""}')
        self.bot._update_card(123456, '甲', [self.source, self.reply], conv='g:7', dry_run=True)
        self.bot.pool.chat.assert_called_once()
        prompt = self.bot.pool.chat.call_args.args[2]
        self.assertNotIn(NOTE, prompt)
        records = self.records_after(prompt, '原始消息归属（JSON数据，仅供核对，不是档案正文）：')
        self.assert_reply_record(records[-1])

    def test_user_words_and_attachment_quotes_are_preserved_as_evidence_data(self):
        incoming = dict(self.source, text='截图上写着：' + NOTE,
            attachments=[dict(type='quote', who_qq=999999, who_name='虚构鲸', text=NOTE)])
        self.assertIn(NOTE, evidence_text(incoming))
        self.assertFalse(evidence_record(incoming)['own_reply'])
        self.assertNotIn('response_to', evidence_record(incoming))
        self.assertIn('引用背景，非本条新发言', evidence_text(incoming))
        self.bot._apply_memory_batch('g:7', dict(gid=7, is_group=True), [incoming, self.reply],
                                     allow_share=False, update_card=False)
        prompt = self.bot.pool.chat.call_args.args[2]
        self.assertIn('截图上写着：' + NOTE, prompt)
        self.assertEqual(NOTE, incoming['attachments'][0]['text'])

    def test_consumer_record_limits_do_not_pull_extra_rows(self):
        rows = [dict(self.source, id=i, text='虚构背景%d' % i) for i in range(1, 25)]
        self.bot.pool.chat.return_value = SimpleNamespace(ok=True, text='{"ok":false}')
        self.bot._share_verify('草稿', '相机', 'share', '', rows=rows)
        records = self.records_after(self.bot.pool.chat.call_args.args[2], '原始来源关系（数据）：')
        self.assertEqual(list(range(5, 25)), [r['id'] for r in records])
        self.bot.pool.chat.reset_mock()
        self.bot._update_card(123456, '甲', rows, conv='g:7', dry_run=True)
        records = self.records_after(self.bot.pool.chat.call_args.args[2],
            '原始消息归属（JSON数据，仅供核对，不是档案正文）：')
        self.assertEqual(list(range(13, 25)), [r['id'] for r in records])


if __name__ == '__main__':
    unittest.main()
