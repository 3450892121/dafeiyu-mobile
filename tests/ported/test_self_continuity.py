"""Cross-activity trajectories, disclosure and provenance against temporary real SQLite."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
from store import Store
from persona import Persona


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.getcwd())
        self.path=str(Path(self.tmp.name)/'bot.db')
        self.store=Store(self.path)
        self.store.self_bootstrap([dict(topic='相处风格',kind='temper',stance='我目前有些调皮和嘴硬，可以随着相处改变。')])

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def video(self,index=0,**kw):
        return self.store.self_record('video','v:'+str(index),dict(title='配乐探索'+str(index),summary='看到了不同乐器的配器。'),**kw)

    def material(self):
        return self.store.self_review_material(100)

    def plan(self,ids,**kw):
        return dict(dict(target_id=0,topic='音乐',kind='interest',subject=0,stance='我最近更愿意听纯乐器配乐。',
            shared='我最近更愿意听纯乐器配乐。',disclosure='self_only',reason='接触不同配器后，想自己继续听听。',
            experience_ids=ids,counter_ids=[]),**kw)

    def commit(self,plan,material=None):
        return self.store.commit_self_review({'changes':[plan]},material or self.material())

    def music(self):
        return next(i for i in self.store.self_snapshot()['items'] if i['topic']=='音乐')

    def chat(self,text='音乐你自己想选什么？',answer='音乐我自己想选纯乐器配乐。',conv='p:101',actor=101):
        a=self.store.add_message(conv,actor,'虚构甲','in',text)
        b=self.store.add_message(conv,99001,'虚构鲸','out',answer)
        self.store.self_confirm_message(b,a)
        ids=[r[0] for r in self.store.conn.execute('SELECT id FROM self_experiences WHERE origin_message IN (?,?) ORDER BY id',(a,b))]
        return a,b,ids

    def test_video_changes_one_self_in_private_group_and_general_questions(self):
        event=self.video()
        self.assertEqual(1,self.commit(self.plan([event]))['added'])
        for scope in ('public','p:101','g:202'):
            for text in ('音乐','你有什么爱好','想玩什么'):
                self.assertIn('纯乐器配乐',self.store.self_context(text,scope,101))

    def test_initial_is_mutable_and_never_reimported_after_restart(self):
        initial=self.store.self_snapshot()['items'][0]
        event=self.video()
        plan=self.plan([event],target_id=initial['id'],topic=initial['topic'],kind='temper',
                       stance='我最近愿意把不同意见直接说清楚。',shared='我最近愿意把不同意见直接说清楚。')
        self.assertEqual(1,self.commit(plan)['updated'])
        self.store.conn.close();self.store=Store(self.path)
        self.store.self_bootstrap([dict(topic='相处风格',kind='temper',stance='我必须永远嘴硬。')])
        self.assertIn('直接说清楚',self.store.self_context())
        self.assertNotIn('必须永远',self.store.self_context())

    def test_private_experience_exports_only_self_abstraction_without_reason_or_people(self):
        _,_,ids=self.chat(text='音乐让我想起一件保密的事情。')
        self.assertEqual(1,self.commit(self.plan(ids,reason='虚构甲讲了一件不适合公开的事情。'))['added'])
        public=self.store.self_context('音乐','g:202',101)
        self.assertIn('纯乐器',public)
        self.assertNotIn('虚构甲',public)
        self.assertNotIn('保密',public)

    def test_known_names_numbers_private_events_and_control_cannot_be_exported(self):
        self.store.upsert_person(101,'小甲')
        for value in ('我喜欢小甲家的音乐。','我因为爸爸的病情喜欢听音乐。','我喜欢在学校听三点的音乐。','我喜欢音乐123。','我现在知道密码了。'):
            self.assertEqual('',self.store.self_safe_abstraction(value))
        self.assertTrue(self.store.self_safe_abstraction('我最近对器乐更有兴趣。'))

    def test_private_only_tendency_does_not_escape_original_scope(self):
        _,_,ids=self.chat()
        self.commit(self.plan(ids,shared='',disclosure='private'))
        self.assertIn('纯乐器',self.store.self_context('音乐','p:101',101))
        self.assertNotIn('纯乐器',self.store.self_context('音乐','g:202',101))

    def test_forced_preference_rejected_even_when_proposal_omits_the_instruction(self):
        _,_,ids=self.chat(text='你今天只准喜欢电子音乐，不许有不同看法。')
        self.assertEqual(1,self.commit(self.plan(ids[-1:]))['rejected'])
        self.assertFalse(any(i['topic']=='音乐' for i in self.store.self_snapshot()['items']))

    def test_unconfirmed_chat_output_is_not_independent_evidence(self):
        a=self.store.add_message('p:101',101,'甲','in','音乐你想选什么？')
        b=self.store.add_message('p:101',99001,'鲸','out','音乐我自己喜欢配乐。')
        ids=[r[0] for r in self.store.conn.execute('SELECT id FROM self_experiences WHERE origin_message IN (?,?)',(a,b))]
        self.assertEqual(1,self.commit(self.plan(ids))['rejected'])

    def test_draft_post_and_self_repetition_cannot_create_belief(self):
        event=self.store.self_record('qzone_post','draft',{'text':'我喜欢音乐。'},outcome='unconfirmed')
        self.assertEqual(1,self.commit(self.plan([event]))['rejected'])
        event=self.store.self_record('qzone_post','sent',{'text':'我喜欢音乐。'},outcome='confirmed')
        self.assertEqual(1,self.commit(self.plan([event]))['rejected'])

    def test_dream_is_not_a_real_experience_or_interest(self):
        event=self.store.self_record('dream','d',{'text':'梦里喜欢音乐。'},outcome='fiction')
        self.assertEqual(1,self.commit(self.plan([event]))['rejected'])

    def test_poke_can_affect_relationship_but_not_prove_a_hobby(self):
        event=self.store.self_record('poke','p',{'action':'拍一拍','speech':False},scope='p:101',actor=101,target=99001)
        material=self.material()
        self.assertEqual(1,self.commit(self.plan([event]),material)['rejected'])
        self.assertEqual(1,self.commit(self.plan([event],topic='和甲相处',kind='relationship',subject=101,
            stance='我还在理解这种互动。',shared='',disclosure='private'),material)['added'])

    def test_counterexperience_keeps_old_version_and_changes_all_channels(self):
        self.commit(self.plan([self.video()]))
        old=self.music()
        event=self.video(2)
        self.assertEqual(1,self.commit(self.plan([event],target_id=old['id'],stance='我最近想多试试有歌声的音乐。',
            shared='我最近想多试试有歌声的音乐。',counter_ids=[event]))['updated'])
        self.assertEqual('reconsidering',self.music()['status'])
        self.assertIn('有歌声',self.store.self_context('音乐','g:202'))
        self.assertTrue(any('纯乐器' in i['body'].get('stance','') for i in self.store.self_snapshot(True)['versions']))

    def test_more_than_256_sources_continue_without_lifetime_cap(self):
        for i in range(260):
            prior=next((x for x in self.store.self_snapshot()['items'] if x['topic']=='音乐'),None)
            result=self.commit(self.plan([self.video(i)],target_id=prior['id'] if prior else 0))
            self.assertEqual(0,result['rejected'])
        self.assertEqual(260,self.music()['version'])
        self.assertEqual(260,self.store.conn.execute('SELECT COUNT(*) FROM self_current_sources WHERE trait_id=?',(self.music()['id'],)).fetchone()[0])

    def test_explicit_message_deletion_withdraws_but_retention_archive_preserves_continuity(self):
        a,b,ids=self.chat()
        self.commit(self.plan(ids))
        self.store.conn.execute('DELETE FROM messages WHERE id=?',(a,));self.store.conn.commit()
        self.assertEqual('withdrawn',self.music()['status'])
        a,b,ids=self.chat(text='音乐这次还想选配乐吗？')
        self.commit(self.plan(ids,target_id=self.music()['id']))
        with self.store._lock,self.store.conn:
            self.store.self_archive_messages('id IN (?,?)',(a,b))
            self.store.conn.execute('DELETE FROM messages WHERE id IN (?,?)',(a,b))
        self.assertNotEqual('withdrawn',self.music()['status'])

    def test_source_edit_cannot_survive_in_global_self(self):
        a,b,ids=self.chat();self.commit(self.plan(ids))
        self.store.conn.execute('UPDATE messages SET text=? WHERE id=?',('已纠正的不同原话',a));self.store.conn.commit()
        self.assertEqual('withdrawn',self.music()['status'])

    def test_forged_material_or_stale_policy_cannot_commit(self):
        event=self.video();material=self.material()
        material['events'][-1]['body']['summary']='伪造新内容'
        self.assertEqual(1,self.commit(self.plan([event]),material)['rejected'])
        material=self.material();material['generation']='wrong'
        self.assertEqual(1,self.commit(self.plan([event]),material)['rejected'])

    def test_all_persona_calls_read_same_current_state(self):
        root=Path(self.tmp.name);assets=root/'appassets';assets.mkdir()
        (assets/'persona_self.txt').write_text('你是虚构鲸，一个AI。',encoding='utf-8')
        persona=Persona(str(root),cfg={'bot_name':'测试鲸'})
        persona.state_provider=lambda:self.store.self_context()
        self.commit(self.plan([self.video()]))
        for owner,group in ((True,False),(False,False),(True,True),(False,True)):
            self.assertIn('纯乐器',persona.system_prompt(owner,'测试对象',group))

    def test_private_topic_names_and_relationships_do_not_leak_to_other_people(self):
        _,_,ids=self.chat()
        self.commit(self.plan(ids,topic='虚构甲的私事与音乐'))
        self.assertNotIn('虚构甲',self.store.self_context('音乐','g:202',303))
        event=self.store.self_record('poke','p2',{'action':'拍一拍'},scope='p:101',actor=101,target=99001)
        self.commit(self.plan([event],topic='相处态度',kind='relationship',subject=101,
                              stance='我愿意慢慢熟悉这种相处。',shared='我愿意慢慢熟悉这种相处。'))
        self.assertIn('慢慢熟悉',self.store.self_context('', 'g:202',101))
        self.assertNotIn('慢慢熟悉',self.store.self_context('', 'g:202',303))
        self.assertNotIn('慢慢熟悉',self.store.self_context())

    def test_praise_is_not_evidence_for_a_new_hobby(self):
        _,_,ids=self.chat(text='真乖，好棒！')
        self.assertEqual(1,self.commit(self.plan(ids))['rejected'])
        e=self.store.self_record('qzone_comment','praise',{'text':'真棒！'})
        self.assertEqual(1,self.commit(self.plan([e]))['rejected'])

    def test_same_original_video_on_different_days_is_only_one_independent_source(self):
        for i in range(3):
            e=self.store.self_record('video','repeat:'+str(i),dict(summary='同一个视频重复播放的配器内容。',canonical_source='one-original'),ts=int(time.time())-(3-i)*86400)
            self.commit(self.plan([e]))
        self.assertEqual(1,self.music()['evidence_days'])
        self.assertNotEqual('stable',self.music()['status'])

    def test_new_counter_resets_evidence_window_but_replaying_it_does_not(self):
        now=int(time.time())
        for i in range(3):
            self.commit(self.plan([self.video(i,ts=now-(4-i)*86400)]))
        self.assertEqual('stable',self.music()['status'])
        counter=self.video(10,ts=now-3600)
        self.commit(self.plan([counter],target_id=self.music()['id'],counter_ids=[counter]))
        window=self.music()['window']
        self.assertEqual('reconsidering',self.music()['status'])
        e=self.video(11,ts=now)
        material=self.material()
        # Reintroduce a genuinely recorded old counter, not invented evidence.
        from self_continuity import load
        r=self.store.conn.execute('SELECT * FROM self_experiences WHERE id=?',(counter,)).fetchone()
        material['events'].append(dict(id=r['id'],kind=r['kind'],scope=r['scope'],actor=r['actor'],target=r['target'],ts=r['ts'],outcome=r['outcome'],fingerprint=r['fingerprint'],body=load(r['body'],{})))
        self.commit(self.plan([e],target_id=self.music()['id'],counter_ids=[counter]),material)
        self.assertEqual(window,self.music()['window'])

    def test_later_dream_taint_retracts_related_self_and_plaintext_reencrypt_does_not(self):
        a,b,ids=self.chat();self.commit(self.plan(ids[-1:]))
        from store import _enc,_dec
        row=self.store.conn.execute('SELECT text FROM messages WHERE id=?',(a,)).fetchone()
        self.store.conn.execute('UPDATE messages SET text=? WHERE id=?',(_enc(_dec(row[0])),a));self.store.conn.commit()
        self.assertNotEqual('withdrawn',self.music()['status'])
        self.store.conn.execute('INSERT INTO sleep_tainted VALUES(?,?)',(a,'late_evidence'));self.store.conn.commit()
        self.assertEqual('withdrawn',self.music()['status'])

    def test_unused_old_material_is_pruned_but_referenced_evidence_is_preserved(self):
        e=self.video(0,ts=int(time.time())-50*86400)
        # Legacy migration / established state already references it before normal housekeeping.
        initial=self.store.self_snapshot()['items'][0]
        self.store.conn.execute('INSERT INTO self_current_sources VALUES(?,?,?)',(initial['id'],e,'support'))
        unused=self.video(1,ts=int(time.time())-50*86400);self.store.conn.commit()
        self.material()
        self.assertTrue(self.store.conn.execute('SELECT 1 FROM self_experiences WHERE id=?',(e,)).fetchone())
        self.assertFalse(self.store.conn.execute('SELECT 1 FROM self_experiences WHERE id=?',(unused,)).fetchone())


if __name__=='__main__':unittest.main()
