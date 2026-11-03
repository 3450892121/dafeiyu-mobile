"""Behavioral checks for grounded night learning, waking recall and fiction isolation."""
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parent))
import test_dreaming as legacy
from sleep_memory import (local, reply_scope, reply_context, note_delivery, guard_dream_reply,
                          finish_reply, validate_plan)
from flow_memory import MemoryMixin


class SleepTests(unittest.TestCase):
    setUp = legacy.DreamTests.setUp
    tearDown = legacy.DreamTests.tearDown
    message = legacy.DreamTests.message
    tick = legacy.DreamTests.tick

    def generate(self, system, prompt, temperature):
        if temperature>=.5:
            if getattr(self,'fail_dream',False):
                return '<think>不该泄漏的分析</think>'*6,'fake'
            if getattr(self,'interrupt_dream',False):
                self.runner.touch()
            return legacy.DreamTests.generate(self,system,prompt,temperature)
        self.calls.append((system,prompt,temperature))
        data=json.loads(prompt.split('\n',1)[1]);sources=data['sources']
        rows=[s for s in sources if s['id'] in data['dream_source_ids']]
        plans=getattr(self,'plans',None)
        if plans is None:
            groups={}
            for row in sources:
                if '考试' in row['text']:
                    groups.setdefault((row['conv'],row['sender_id']),[]).append(row['id'])
            plans=[{'topic':'考试','source_ids':ids[:8],'kind':'progress','lesson':'none'} for ids in groups.values()]
        return json.dumps({'moments':[{'source_id':r['id'],'quote':r['text'][:240],'theme':'进展'} for r in rows],
                           'consolidations':plans},ensure_ascii=False),'fake'

    def seed_progress(self,conv='p:1'):
        a=self.message('这次考试我还在准备，报名费是两百元',conv,ts=self.now-3*86400)
        b=self.message('这次考试成绩出来了，我通过了，分数是85分',conv)
        return [a,b]

    def advance(self):
        self.clock.stop();self.now+=86400
        self.clock=patch('dreaming.time.time',return_value=self.now);self.clock.start()
        self.runner.last_activity=self.now-3600

    def test_night_creates_retrievable_progress_with_exact_numbers(self):
        self.seed_progress();self.assertEqual('complete',self.tick())
        row=self.store.conn.execute('SELECT * FROM sleep_memories').fetchone()
        self.assertEqual('completed',row['state'])
        block,ids=self.store.sleep_context('p:1',1,'考试结果怎么样')
        self.assertIn('两百元',block);self.assertIn('85分',block);self.assertIn('通过了',block)
        self.assertEqual([row['id']],ids)
        effects=self.store.dream_state(self.now)['runs'][0]['effects']
        self.assertEqual(1,effects['added']);self.assertEqual(1,len(effects['records']))

    def test_context_reset_does_not_lose_consolidated_memory(self):
        self.seed_progress();self.tick();self.store.reset_session('p:1')
        self.assertIn('85分',self.store.sleep_context('p:1',1,'考试')[0])

    def test_next_day_updates_a_version_instead_of_duplicate(self):
        self.seed_progress();self.tick();self.advance()
        self.message('纠正一下考试分数，说错了，其实是89分',ts=self.now-2000)
        self.assertEqual('complete',self.tick())
        self.assertEqual(1,self.store.conn.execute('SELECT count(*) FROM sleep_memories').fetchone()[0])
        row=self.store.conn.execute('SELECT version,state FROM sleep_memories').fetchone()
        self.assertEqual((2,'corrected'),tuple(row))
        self.assertEqual(2,self.store.conn.execute('SELECT count(*) FROM sleep_memory_versions').fetchone()[0])
        body=self.store.sleep_context('p:1',1,'考试')[0]
        self.assertIn('85分',body);self.assertIn('89分',body)
        self.assertLess(body.index('85分'),body.index('89分'))

    def test_latest_daytime_correction_is_visible_before_next_night(self):
        self.seed_progress();self.tick()
        self.message('考试结果我刚才看错了，其实没有通过',ts=self.now)
        block,_=self.store.sleep_context('p:1',1,'考试结果呢')
        self.assertIn('其实没有通过',block)
        self.assertGreater(block.index('其实没有通过'),block.index('我通过了'))

    def test_negated_result_is_not_marked_completed(self):
        self.message('这次考试我还在准备，报名费是两百元',ts=self.now-86400)
        self.message('这次考试没有通过，我还没完成这件事')
        self.assertEqual('complete',self.tick())
        self.assertEqual('ongoing',self.store.conn.execute('SELECT state FROM sleep_memories').fetchone()[0])

    def test_private_group_and_other_speaker_boundaries(self):
        self.seed_progress();self.tick()
        for conv,uid in [('p:2',2),('g:2',1),('g:3',1)]:
            self.assertEqual(('',[]),self.store.sleep_context(conv,uid,'考试结果呢'))
            block,rid=self.store.dream_recall(conv,uid,'你昨晚梦见什么')
            self.assertEqual(0,rid);self.assertNotIn('饭团',block)

    def test_cross_scope_plan_is_rejected_atomically(self):
        ids=self.seed_progress();other=self.message('考试结果出来了，我考了100分','g:2')
        self.plans=[{'topic':'考试','source_ids':[ids[0],other],'kind':'progress'}]
        self.assertEqual('failed',self.tick())
        self.assertEqual(0,self.store.conn.execute('SELECT count(*) FROM sleep_memories').fetchone()[0])

    def test_same_group_different_people_not_merged(self):
        a=self.message('这次考试我还在准备，要交两百元','g:2')
        b=self.store.add_message('g:2',2,'小乙','in','这次考试我已经通过，分数90分',ts=self.now-1900)
        self.plans=[{'topic':'考试','source_ids':[a,b],'kind':'progress'}]
        self.assertEqual('failed',self.tick())

    def test_unsupported_summary_topic_cannot_be_written(self):
        ids=self.seed_progress();self.plans=[{'topic':'主人不在乎我','source_ids':ids,'kind':'fact'}]
        self.assertEqual('failed',self.tick())
        self.assertEqual(0,self.store.conn.execute('SELECT count(*) FROM sleep_commits').fetchone()[0])

    def test_explicit_long_term_communication_preference_changes_context(self):
        mid=self.message('以后你回复简短一点，不要每次都长篇大论')
        self.plans=[{'topic':'回复','source_ids':[mid],'kind':'preference','lesson':'concise'}]
        self.assertEqual('complete',self.tick())
        block,_=self.store.sleep_context('p:1',1,'今天吃什么')
        self.assertIn('沟通偏好',block);self.assertIn('先回答要点',block)
        self.assertEqual(('',[]),self.store.sleep_context('g:9',1,'今天吃什么'))

    def test_one_off_formatting_request_not_a_permanent_lesson(self):
        a=self.message('这次回复简短一点，不要长篇大论')
        b=self.message('我的回复就是刚才那条说明，你看到了吗')
        self.plans=[{'topic':'回复','source_ids':[a,b],'kind':'preference','lesson':'concise'}]
        self.assertEqual('complete',self.tick())
        self.assertEqual('none',self.store.conn.execute('SELECT lesson FROM sleep_memories').fetchone()[0])
        self.assertEqual(('',[]),self.store.sleep_context('p:1',1,'晚饭吃什么'))

    def test_dream_failure_does_not_erase_successful_learning(self):
        self.seed_progress();self.fail_dream=True
        self.assertEqual('failed',self.tick())
        self.assertIn('85分',self.store.sleep_context('p:1',1,'考试')[0])
        self.assertEqual(1,self.store.dream_state(self.now)['runs'][0]['effects']['added'])

    def test_new_message_after_learning_discards_only_the_dream(self):
        self.seed_progress();self.interrupt_dream=True
        self.assertEqual('interrupted',self.tick())
        self.assertEqual(0,self.store.conn.execute('SELECT count(*) FROM dream_entries').fetchone()[0])
        self.assertEqual(1,self.store.conn.execute('SELECT count(*) FROM sleep_memories').fetchone()[0])

    def test_database_error_rolls_back_the_whole_learning_commit(self):
        self.seed_progress()
        self.store.conn.execute("CREATE TRIGGER reject_sleep BEFORE INSERT ON sleep_memory_versions BEGIN SELECT RAISE(ABORT,'test failure'); END")
        self.assertEqual('failed',self.tick())
        self.assertEqual(0,self.store.conn.execute('SELECT count(*) FROM sleep_memories').fetchone()[0])

    def test_source_deletion_withdraws_derived_memory_and_historical_text(self):
        ids=self.seed_progress();self.tick()
        self.store.conn.execute('DELETE FROM messages WHERE id=?',(ids[0],));self.store.conn.commit()
        self.assertEqual(('',[]),self.store.sleep_context('p:1',1,'考试'))
        row=self.store.conn.execute('SELECT status,body,sources FROM sleep_memories').fetchone()
        self.assertEqual(('withdrawn','','[]'),tuple(row))
        self.assertEqual('',self.store.conn.execute('SELECT body FROM sleep_memory_versions').fetchone()[0])

    def test_long_term_learning_outlives_the_dream_diary_retention(self):
        self.seed_progress();self.tick();later=self.now+31*86400
        self.store.dream_state(later)
        self.assertEqual(0,self.store.conn.execute('SELECT count(*) FROM dream_entries').fetchone()[0])
        self.assertIn('85分',self.store.sleep_context('p:1',1,'考试',now=later)[0])

    def test_dream_is_recalled_only_when_related_or_explicitly_asked(self):
        self.seed_progress();self.tick()
        self.assertEqual(('',0),self.store.dream_recall('p:1',1,'今天天气不错',self.now+3600))
        block,rid=self.store.dream_recall('p:1',1,'这次考试通过了',self.now+3600)
        self.assertGreater(rid,0);self.assertIn('虚构',block)
        self.assertEqual(('',0),self.store.dream_recall('p:1',1,'这次考试通过了',self.now+3601))
        self.assertGreater(self.store.dream_recall('p:1',1,'你昨晚梦见什么',self.now+3602)[1],0)

    def test_expiry_and_request_not_to_mention_dream(self):
        self.seed_progress();self.tick()
        self.assertEqual(('',0),self.store.dream_recall('p:1',1,'这次考试通过了',self.now+13*3600))
        self.assertEqual(('',0),self.store.dream_recall('p:1',1,'不要再说梦境了',self.now+3600))
        self.assertEqual(0,self.store.dream_recall('p:1',1,'你昨晚梦见什么',self.now+8*86400)[1])

    def test_no_saved_dream_does_not_invite_fabrication(self):
        block,rid=self.store.dream_recall('p:1',1,'你昨晚梦见什么')
        self.assertEqual(0,rid);self.assertIn('不能现编',block)

    def test_actual_memory_prompt_and_search_tool_use_the_sleep_memory(self):
        self.seed_progress();self.tick()
        class Bot(MemoryMixin):
            cfg={}
            def _canonical_name(self,uid,name):return name
            def _qzone_memory_section(self):return ''
        bot=Bot();bot.store=self.store
        block=bot.build_memory_block(1,'小甲',False,0,'考试结果')
        self.assertIn('85分',block)
        found=bot.memory_search_tool('all','考试',False,0,1,False)
        self.assertIn('85分',found)

    def test_quoted_dream_is_not_learned_or_scored_as_real_betrayal(self):
        self.seed_progress();self.tick()
        @reply_scope
        def answer(bot,conv,uid,ev):
            reply_context(bot,conv,uid,'你昨晚梦见什么')
            note_delivery(bot.store,{'message_id':'dream-receipt-1'})
            bot.store.add_message(conv,99,'测试鲸','out','梦里我拿着试卷追小鱼')
        answer(self.bot,'p:1',1,{'message_id':'ask-1'})
        mid=self.store.add_message('p:1',1,'小甲','in','你刚才说我泄露你的秘密，我真的做过吗',message_id=10001,
                                   attachments=[{'type':'quote','id':'dream-receipt-1','text':'我拿着试卷追小鱼'}])
        self.assertTrue(self.store.sleep_tainted(mid))
        decision={'kind':'betrayal','target':'bot','evidence':'我泄露你的秘密','confidence':1}
        self.assertIsNone(self.store.apply_interaction(1,'p:1',10001,'你刚才说我泄露你的秘密，我真的做过吗',decision,now=self.now))
        self.assertEqual(0,self.store.conn.execute('SELECT count(*) FROM interaction_events').fetchone()[0])
        self.assertNotIn(mid,[s['id'] for s in self.store.sleep_material(self.now+1)])
        class Bot(MemoryMixin):pass
        b=Bot();b.store=self.store
        tainted=[dict(r) for r in self.store.conn.execute('SELECT * FROM messages WHERE id=?',(mid,))]
        self.assertEqual(0,b._apply_memory_batch('p:1',{'uid':1},tainted,allow_share=False,update_card=False))

    def test_successful_delivery_records_usage_without_relearning_the_reply(self):
        self.seed_progress();self.tick()
        @reply_scope
        def answer(bot,conv,uid,ev):
            reply_context(bot,conv,uid,'你昨晚梦见什么')
            body=self.store.conn.execute('SELECT text FROM dream_entries').fetchone()[0]
            text='我梦见了，'+body
            bot.store.add_message(conv,99,'测试鲸','out',text)
        answer(self.bot,'p:1',1,{'message_id':'ask-2'})
        self.assertIsNone(getattr(local,'reply',None))
        self.assertGreater(self.store.dream_state(self.now)['runs'][0]['effects']['mentioned'],0)
        self.assertEqual(0,len(self.store.person_memories(1)) if hasattr(self.store,'person_memories') else self.store.conn.execute('SELECT count(*) FROM person_memories').fetchone()[0])

    def test_an_offered_dream_does_not_label_an_ordinary_answer_as_a_dream(self):
        self.seed_progress();self.tick()
        @reply_scope
        def answer(bot,conv,uid,ev):
            reply_context(bot,conv,uid,'这次考试通过了')
            return guard_dream_reply('恭喜，报名的钱没白交。')
        self.assertEqual('恭喜，报名的钱没白交。',answer(self.bot,'p:1',1,{'message_id':5}))

    def test_unchanged_evidence_does_not_claim_a_new_learning_gain(self):
        self.seed_progress();self.tick()
        sources=self.store.sleep_material(self.now)
        self.assertEqual([],sources)
        row=self.store.conn.execute('SELECT version FROM sleep_memories').fetchone()
        self.assertEqual(1,row[0])

    def test_original_protected_memory_and_relationships_are_untouched(self):
        item=self.store.add_person_memory(1,'小甲重要约定：周五给家里打电话','commitment',5)
        self.store.set_memory_protected('person_memories',item['id'],True)
        tables=('person_memories','person_relations','interaction_events','person_emotions','reminders')
        before={t:[tuple(r) for r in self.store.conn.execute('SELECT * FROM '+t)] for t in tables}
        self.seed_progress();self.tick()
        self.assertEqual(before,{t:[tuple(r) for r in self.store.conn.execute('SELECT * FROM '+t)] for t in tables})

    def test_latest_explicit_style_reversal_overrides_old_lesson(self):
        mid=self.message('以后你回复简短一点，不要每次都长篇大论')
        self.plans=[{'topic':'回复','source_ids':[mid],'kind':'preference','lesson':'concise'}]
        self.tick()
        self.message('以后回答详细一点，把过程展开讲清楚',ts=self.now)
        self.assertNotIn('先回答要点',self.store.sleep_context('p:1',1,'晚饭吃什么')[0])

    def test_all_scopes_can_learn_but_do_not_share_their_details(self):
        self.seed_progress('g:2')
        self.message('考试还在准备，报名费用300元','g:3',ts=self.now-86400)
        self.message('考试通过了，最后是90分','g:3')
        self.assertEqual('complete',self.tick())
        self.assertEqual(2,self.store.conn.execute('SELECT count(*) FROM sleep_memories').fetchone()[0])
        a=self.store.sleep_context('g:2',1,'考试')[0];b=self.store.sleep_context('g:3',1,'考试')[0]
        self.assertIn('85分',a);self.assertNotIn('90分',a)
        self.assertIn('90分',b);self.assertNotIn('85分',b)

    def test_dream_discussion_does_not_enter_diary_or_relationship_context(self):
        mid=self.message('我梦见你偷走了我的考试试卷')
        self.assertNotIn(mid,[r['id'] for r in self.store.recent_rows('p:1')])
        self.assertEqual([],self.store.recent_msgs_by_conv('p:1'))
        self.assertEqual([],self.store.recent_msgs_for_person(1))
        self.assertEqual([],self.store.person_recent_messages(1))
        day=__import__('dreaming').local_day(self.now)
        self.assertFalse(self.store.day_messages(day))
        from store import _render_attachment_notes
        import guard
        rendered=_render_attachment_notes('我拿着试卷追小鱼',[{'type':'dream'}],'out')
        self.assertIn('不作现实事实',rendered)
        self.assertEqual('我拿着试卷追小鱼',guard.scrub(rendered))

    def test_event_ledger_links_a_short_followup_without_inventing_facts(self):
        a=self.store.add_message('p:1',1,'小甲','in','考试还在准备，要交200元',message_id=101,ts=self.now-86400)
        b=self.store.add_message('p:1',1,'小甲','in','结果出来了，85分，已经通过了',message_id=102,ts=self.now-2000)
        from store import _enc
        self.store.conn.execute("INSERT INTO interaction_events(qq,conv,source_mid,source,evidence,kind,topic,state,prior_id,effects,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(1,'p:1','101',_enc('考试还在准备，要交200元'),_enc('考试还在准备'),'neutral',_enc('考试'),'active',0,'{}',int(self.now-86400)))
        event_id=self.store.conn.execute('SELECT max(id) FROM interaction_events').fetchone()[0]
        self.store.conn.execute("INSERT INTO interaction_events(qq,conv,source_mid,source,evidence,kind,topic,state,prior_id,effects,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(1,'p:1','102',_enc('结果出来了，85分，已经通过了'),_enc('已经通过了'),'good_news',_enc('考试'),'completed',event_id,'{}',int(self.now-2000)))
        self.store.conn.commit()
        self.plans=[{'topic':'考试','source_ids':[a,b],'kind':'progress'}]
        self.assertEqual('complete',self.tick())
        self.assertIn('85分',self.store.sleep_context('p:1',1,'考试')[0])

    def test_relevant_progress_is_not_crowded_out_by_style_lessons(self):
        ids=self.seed_progress()
        a=self.message('以后你回答简短一点，不要每次都长篇大论')
        b=self.message('以后别一直追问，我说明白了就接着聊')
        self.plans=[{'topic':'回答','source_ids':[a],'kind':'preference','lesson':'concise'},
                    {'topic':'追问','source_ids':[b],'kind':'preference','lesson':'no_followup'},
                    {'topic':'考试','source_ids':ids,'kind':'progress'}]
        self.assertEqual('complete',self.tick())
        self.assertIn('85分',self.store.sleep_context('p:1',1,'考试结果呢')[0])


if __name__=='__main__':unittest.main()
