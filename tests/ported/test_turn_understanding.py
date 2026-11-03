"""Fictional conversations, temporary SQLite and fake models; no real transports."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get('BOT_TEST_PATH') or str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
from store import Store
from conversation_understanding import (capture_inbound, start_capture, turn_frame,
    frame_note, evidence_text, complete_summary, valid_memory_evidence, linked_reply)
from flow_stickers import parse_sticker_request
from flow_media import draw_hint
from flow_intent import IntentMixin
from model import ModelPool, Result
import reply_autonomy as autonomy


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name)/'bot.db'))
        self.addCleanup(self.store.conn.close)

    def test_fast_reply_keeps_input_before_output_and_addressee(self):
        class Bot:
            def __init__(self, store): self.store = store
            @capture_inbound
            def reply(self, conv, uid, name, text, ev, images=(), files=(), records=(), quote=None):
                start_capture()
                self.store.add_message(conv, 99, '虚构鲸', 'out', '我知道你在要求改发色')
        bot = Bot(self.store)
        ev = dict(message_id=31, time=100)
        bot.reply('g:7', 10, '甲', '换个发色再发图片', ev, quote=dict(id=29, who_qq=99, text='刚才的图'))
        rows = self.store.thread_rows('g:7', 10)
        self.assertEqual(['in','out'], [r['direction'] for r in rows])
        self.assertEqual('换个发色再发图片', rows[0]['text'])
        self.assertEqual(99, rows[0]['attachments'][0]['who_qq'])
        history = self.store.messages_for_context('g:7', 10)
        self.assertEqual('我知道你在要求改发色', history[-1]['content'])
        self.assertEqual('assistant', history[-1]['role'])
        self.assertEqual(10, history[-1]['response_to']['who_qq'])
        frame = turn_frame(self.store, 'g:7', 20, '乙', '我也是', self_id=99)
        own = frame['timeline'][-1]
        self.assertEqual(99, own['speaker'])
        self.assertTrue(own['own_reply'])
        self.assertEqual(rows[0]['id'], own['response_to']['source_id'])
        self.assertEqual(10, own['response_to']['who_qq'])
        self.assertEqual(31, own['response_to']['message_id'])
        self.assertEqual('甲', own['response_to']['who_name'])
        self.assertNotIn('接话对象', own['text'])
        self.assertEqual(10, rows[-1]['attachments'][-1]['who_qq'])

    def test_old_leaked_note_is_hidden_from_context_without_editing_history(self):
        original = '好啦，记住了。（此回复的接话对象：甲(QQ10)，对应来话#41）'
        self.store.add_message('g:7', 99, '虚构鲸', 'out', original,
            attachments=[dict(type='response_to', source_id=41, who_qq=10, who_name='甲')])
        self.store.add_message('g:7', 20, '乙', 'in', '我引用的原话：' + original)
        history = self.store.messages_for_context('g:7', 10)
        self.assertEqual('好啦，记住了。', history[0]['content'])
        self.assertIn(original, history[1]['content'])
        rows = self.store.thread_rows('g:7', 10)
        self.assertEqual(original, rows[0]['text'])
        frame = turn_frame(self.store, 'g:7', 20, '乙', '继续', self_id=99)
        self.assertEqual('好啦，记住了。', frame['timeline'][0]['text'])
        self.assertEqual(41, frame['timeline'][0]['response_to']['source_id'])
        self.assertIn(original, frame['timeline'][1]['text'])

    def test_normal_input_not_duplicated_and_exception_still_preserves_it(self):
        class Bot:
            def __init__(self, store): self.store=store
            @capture_inbound
            def reply(self, conv, uid, name, text, ev):
                start_capture()
                self.store.add_message(conv, uid, name, 'in', text, message_id=ev['message_id'])
                raise ValueError('fixture failure')
        with self.assertRaises(ValueError):
            Bot(self.store).reply('p:10',10,'甲','不是那个意思',dict(message_id=32))
        self.assertEqual(1,len(self.store.thread_rows('p:10',10)))

    def test_control_and_test_events_do_not_become_chat(self):
        calls=[]
        class Bot:
            def __init__(self, store): self.store=store
            @capture_inbound
            def reply(self, conv, uid, name, text, ev): calls.append(text)
        bot=Bot(self.store)
        bot.reply('p:10',10,'甲','控制命令',dict(message_id=33))
        bot.reply('p:10',10,'甲','测试',dict(message_id=34,_test=True))
        self.assertEqual(['控制命令'],calls)
        self.assertEqual([],self.store.thread_rows('p:10',10))

    def test_frame_anchors_current_person_quote_and_other_persons_project(self):
        self.store.add_message('g:7',10,'甲','in','我的相机正在修',message_id=1)
        self.store.add_message('g:7',20,'乙','in','那位化学教授回答不了',message_id=2)
        self.store.add_message('g:7',20,'乙','in','这个领域的大佬',message_id=3)
        frame=turn_frame(self.store,'g:7',20,'乙','这个领域的大佬',
                         dict(who_qq=10,text='我的相机正在修'),99,3)
        self.assertEqual([20],[r['speaker'] for r in frame['same_speaker_recent']])
        self.assertEqual(10,frame['timeline'][0]['speaker'])
        self.assertEqual(10,frame['explicit_quote']['who_qq'])
        self.assertNotIn(3,[r['mid'] for r in frame['timeline']])
        self.assertIn('不自动变成夸你',frame_note(frame))

    def test_memory_evidence_distinguishes_quote_from_new_words(self):
        text=evidence_text(dict(text='我说的不是这件事',attachments=[dict(type='quote',who_qq=20,who_name='乙',text='猜测的事')]))
        self.assertIn('非本条新发言',text)
        self.assertIn('QQ20',text)

    def test_memory_cannot_claim_another_speakers_or_assistants_evidence(self):
        rows=[dict(id=1,direction='in',sender_id=10,text='我的相机'),
              dict(id=2,direction='in',sender_id=20,text='我的台灯'),
              dict(id=3,direction='out',sender_id=99,text='我猜甲喜欢台灯')]
        self.assertTrue(valid_memory_evidence(dict(evidence=[1]),rows,10))
        for item in ({},dict(evidence=[2]),dict(evidence=[3]),dict(evidence=[999]),dict(evidence=[True])):
            self.assertFalse(valid_memory_evidence(item,rows,10),item)

    def test_sharing_does_not_pair_another_persons_next_reply_with_evidence(self):
        source=dict(id=1,direction='in',sender_id=10,text='我的相机')
        others=[dict(id=2,direction='in',sender_id=20,text='我的台灯'),
                dict(id=3,direction='out',text='台灯不错')]
        self.assertEqual('',linked_reply(source,others))
        others.append(dict(id=4,direction='out',text='相机修好了',attachments=[dict(type='response_to',source_id=1)]))
        self.assertEqual('相机修好了',linked_reply(source,others))

    def test_transform_is_not_a_sticker_request_and_existing_commands_remain(self):
        for text in ('头发换成金色然后发张图片','换一个颜色，然后发一张图片',
                     '要是你头发变成其他颜色的图片','请改一下这张图的背景'):
            self.assertIsNone(parse_sticker_request(text),text)
            self.assertTrue(draw_hint(text),text)
        self.assertEqual({'kw':'开心'},parse_sticker_request('发个开心的表情包'))
        self.assertIsNotNone(parse_sticker_request('发个变颜色的表情包'))
        self.assertIsNone(parse_sticker_request('不要发图'))

    def test_incomplete_or_ambiguous_summary_does_not_advance_as_valid_material(self):
        for text in ('甲说','有人提到想买相机。','“我”提醒大家。'):
            self.assertEqual('',complete_summary(text))
        self.assertEqual('甲(QQ10)在修相机。',complete_summary('甲(QQ10)在修相机。'))
        self.assertEqual('甲说完。',complete_summary('甲说完。'+('乙'*30)+'。',10))
        pool=ModelPool.__new__(ModelPool)
        pool.chat=lambda *a,**k:Result(True,'甲(QQ10)在修相机。',finish='length')
        self.assertEqual('',pool.summarize('甲(QQ10)：修相机'))


class RestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(str(Path(self.tmp.name)/'bot.db'))
        self.addCleanup(self.store.conn.close)
        self.bot=SimpleNamespace(store=self.store,cfg=dict(reply_autonomy_enabled=True),self_id=99)

    def choice(self, mode='sleep'):
        return dict(action='quiet',mode=mode,minutes=180,reason='low_energy')

    def test_saying_goodnight_does_not_force_rest_but_delivered_choice_can(self):
        ev={'_autonomy_choice':dict(action='reply',mode='engaged',minutes=5,reason='company'),
            '_autonomy_committed_revision':0}
        autonomy.note_delivery(self.bot,'p:10',ev,'晚安')
        self.assertEqual('available',autonomy.snapshot(self.store,self.bot.cfg)['mode'])
        ev['_autonomy_choice'].update(after_reply_mode='sleep',after_reply_minutes=180)
        autonomy.note_delivery(self.bot,'p:10',ev,'')
        self.assertEqual('available',autonomy.snapshot(self.store,self.bot.cfg)['mode'])
        autonomy.note_delivery(self.bot,'p:10',ev,'晚安，先歇了')
        self.assertEqual('sleep',autonomy.snapshot(self.store,self.bot.cfg)['mode'])

    def test_rest_survives_restart_repeated_calls_do_not_force_waking(self):
        autonomy._commit(self.bot,'p:10',self.choice(),0)
        state=autonomy.snapshot(self.store,self.bot.cfg)
        self.assertTrue(state['quiet'])
        for n in range(3):
            contact=autonomy.contact_while_resting(self.bot,'p:10',10,state)
            self.assertEqual(n+1,contact['calls_from_current_person'])
        self.assertEqual('sleep',autonomy.snapshot(self.store,self.bot.cfg)['mode'])
        self.assertEqual(1,autonomy.contact_while_resting(self.bot,'g:7',10,state)['contacts_in_30min'])
        note=autonomy.reply_prompt(self.bot,dict(_autonomy_rest_contact=contact))
        self.assertIn('被叫醒',note)
        self.assertIn('由你自己决定',note)

    def test_newer_choice_wins_over_slow_goodnight_delivery(self):
        ev=dict(_autonomy_choice=dict(after_reply_mode='sleep',after_reply_minutes=180),
                _autonomy_committed_revision=0)
        autonomy._commit(self.bot,'p:20',dict(action='reply',mode='engaged',minutes=5,reason='interest'),0)
        autonomy.note_delivery(self.bot,'p:10',ev,'晚安')
        self.assertEqual('engaged',autonomy.snapshot(self.store,self.bot.cfg)['mode'])

    def test_sleep_control_validation(self):
        self.assertEqual('sleep',autonomy._parse(json.dumps(self.choice()))['mode'])
        self.assertIsNone(autonomy._parse(json.dumps(dict(self.choice(),minutes=10000))))
        self.assertIsNone(autonomy._parse(json.dumps(dict(self.choice(),action='reply'))))

    def test_sleep_still_receives_messages_and_each_invitation_gets_a_new_choice(self):
        from unittest.mock import Mock
        self.bot.persona=SimpleNamespace(owner='虚构资料')
        self.bot.pool=SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True,text=json.dumps(self.choice()))))
        autonomy._commit(self.bot,'p:10',self.choice(),0)
        with patch('interaction_state.observe'):
            for mid in (1,2):
                ev=dict(message_id=mid)
                self.assertFalse(autonomy.choose(self.bot,'p:10',10,'甲',True,False,'还在吗',ev))
                autonomy.remember_silence(self.bot,'p:10',10,'甲','还在吗',[],[],[],ev)
        self.assertEqual(2,len(self.store.thread_rows('p:10',10)))
        self.bot.pool.chat.return_value.text=json.dumps(dict(action='reply',mode='engaged',minutes=3,reason='company'))
        ev=dict(message_id=3)
        self.assertTrue(autonomy.choose(self.bot,'p:10',10,'甲',True,False,'想跟你说句话',ev))
        self.assertEqual(3,ev['_autonomy_rest_contact']['calls_from_current_person'])
        self.assertIn('被叫醒',autonomy.reply_prompt(self.bot,ev))
        self.assertEqual(3,self.bot.pool.chat.call_count)

    def test_decision_failure_does_not_force_a_sleeping_bot_to_reply(self):
        self.bot.persona=SimpleNamespace(owner='虚构资料')
        self.bot.pool=SimpleNamespace(chat=lambda *a,**k:SimpleNamespace(ok=False,text=''))
        autonomy._commit(self.bot,'p:10',self.choice(),0)
        self.assertFalse(autonomy.choose(self.bot,'p:10',10,'甲',True,False,'在吗',{}))
        self.assertEqual('sleep',autonomy.snapshot(self.store,self.bot.cfg)['mode'])

    def test_autonomy_gets_separate_accurate_reply_attribution(self):
        from unittest.mock import Mock
        self.store.add_message('g:7',10,'甲','in','我的相机在修',message_id=11)
        self.store.add_message('g:7',99,'虚构鲸','out','你的相机修好再带来',
            attachments=[dict(type='response_to',source_id=1,message_id=11,who_qq=10,who_name='甲')])
        self.bot.persona=SimpleNamespace(owner='虚构资料')
        self.bot.pool=SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True,text=json.dumps(self.choice()))))
        self.assertFalse(autonomy.choose(self.bot,'g:7',20,'乙',False,True,'我也有相机',{}))
        payload=json.loads(self.bot.pool.chat.call_args.args[2])
        linked=payload['历史回复归属（只核对，不要求回复）'][0]
        self.assertEqual(99,linked['speaker'])
        self.assertTrue(linked['own_reply'])
        self.assertEqual(10,linked['response_to']['who_qq'])
        self.assertEqual('你的相机修好再带来',payload['近期会话（只作背景）'][-1]['content'])
        self.store.set_setting('ctx_start:g:7','999')
        autonomy.choose(self.bot,'g:7',20,'乙',False,True,'再说一遍',{})
        payload=json.loads(self.bot.pool.chat.call_args.args[2])
        self.assertEqual([],payload['历史回复归属（只核对，不要求回复）'])


if __name__=='__main__':unittest.main()
