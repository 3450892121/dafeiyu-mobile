import os, sys, tempfile, time, unittest
sys.path.insert(0, os.environ.get('BOT_TEST_PATH') or os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'runtime', 'core')))
from store import Store


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.getcwd())
        self.s=Store(os.path.join(self.tmp.name,'bot.db')); self.now=int(time.time())

    def tearDown(self):
        self.s.conn.close(); self.tmp.cleanup()

    def apply(self, kind='support', mid='m1', text='我陪你一起解决', **extra):
        data=dict(kind=kind,evidence=text,confidence=.95,target='bot',quoted=False,negated=False)
        data.update(extra)
        return self.s.apply_interaction(1,'p:1',mid,text,data,self.now)

    def test_once_and_coupled(self):
        self.assertTrue(self.apply()); self.assertIsNone(self.apply())
        rel=self.s.get_person_relation(1)
        self.assertEqual((52,31),(rel['favor'],rel['trust']))
        self.assertTrue(any(x['kind']=='happy' for x in self.s.get_emotions(1)))

    def test_wrong_target_quote_negation_and_missing_evidence(self):
        for override in ({'target':'other'},{'quoted':True},{'negated':True},{'evidence':'不是当前原话'},{'confidence':.5}):
            self.assertIsNone(self.apply(kind='betrayal',**override))
        self.assertIsNone(self.s.get_person_relation(1))

    def test_user_sad_does_not_make_bot_sad(self):
        self.apply('user_feeling',text='我今天很难过',target='self')
        self.assertEqual([],self.s.get_emotions(1))
        self.assertIsNone(self.s.get_person_relation(1))

    def test_apology_does_not_restore_trust_or_erase_grudge(self):
        self.apply('betrayal',text='我真的把你托付的秘密泄露给别人了')
        before=self.s.get_person_relation(1)['trust']
        self.apply('apology','m2','对不起我做错了')
        self.assertEqual(before,self.s.get_person_relation(1)['trust'])
        self.assertEqual(1,len(self.s.list_grudges(1)))
        self.assertLess(next(x['intensity'] for x in self.s.get_emotions(1) if x['kind']=='upset'),20)

    def test_correction_reverses_only_source_once(self):
        event=self.apply('betrayal',text='我泄露了你的秘密')
        self.apply('boundary','m2','我故意继续越过你的边界')
        before=self.s.get_person_relation(1)['trust']
        self.apply('correction','m3','刚才那句是剧本台词，不是真实的泄密',prior_id=event)
        corrected=self.s.get_person_relation(1)['trust']
        self.assertAlmostEqual(before+3,corrected,delta=.01)
        self.assertEqual(1,len(self.s.list_grudges(1)))
        self.apply('correction','m4','再说明一下之前是剧本',prior_id=event)
        self.assertEqual(corrected,self.s.get_person_relation(1)['trust'])

    def test_unrelated_correction_rolls_back(self):
        with self.assertRaises(ValueError): self.apply('correction',prior_id=999)
        self.assertEqual([],self.s.interaction_events())

    def test_private_events_are_not_group_context(self):
        self.apply()
        self.assertEqual([],self.s.interaction_events(1,'g:2'))
        self.assertEqual([],self.s.interaction_events(2,'p:2'))

    def test_daily_caps(self):
        for i in range(20): self.apply(mid=str(i))
        rel=self.s.get_person_relation(1)
        self.assertEqual((55,32),(rel['favor'],rel['trust']))
        self.assertLessEqual(next(x['intensity'] for x in self.s.get_emotions(1) if x['kind']=='happy'),30)

    def test_transaction_failure_is_atomic(self):
        self.s.conn.execute("CREATE TRIGGER fail_event BEFORE INSERT ON interaction_events BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(Exception): self.apply()
        self.assertIsNone(self.s.get_person_relation(1))
        self.assertEqual([],self.s.get_emotions(1))
        self.assertEqual(0,self.s.conn.execute('SELECT COUNT(*) FROM favor_history').fetchone()[0])

    def test_other_axis_decays_before_timestamp_refresh(self):
        self.s.conn.execute('INSERT INTO person_relations(qq,favor,trust,updated_at) VALUES(1,50,90,?)',(self.now-180*86400,)); self.s.conn.commit()
        self.apply('praise')
        self.assertAlmostEqual(60,self.s.get_person_relation(1)['trust'],delta=.01)

    def test_restatement_does_not_repeat_effects(self):
        event=self.apply()
        self.apply(mid='m2',prior_id=event)
        self.assertEqual(52,self.s.get_person_relation(1)['favor'])

    def test_meaningful_familiarity_counts_days_not_message_volume(self):
        for i in range(8): self.apply(mid=str(i))
        self.assertEqual(2,self.s.meaningful_familiarity(1))
        self.assertEqual({'favor':5.0,'trust':2.0},self.s.interaction_budget(1))

    def test_completed_progress_is_linked(self):
        original=self.apply('neutral',text='明天我要参加考试',topic='数学考试',target='self')
        self.apply('good_news','m2','考试成绩出来，我通过了',topic='数学考试',state='completed',prior_id=original,target='self')
        rows=self.s.interaction_events(1,'p:1')
        self.assertTrue(all(x['state']=='completed' for x in rows))
        self.assertEqual(original,rows[0]['prior_id'])

    def test_group_mood_is_atomic_bounded_and_correctable(self):
        decision=dict(kind='praise',evidence='你真棒',confidence=.99,target='bot')
        event=self.s.apply_interaction(1,'g:2','m1','你真棒',decision,self.now)
        self.assertEqual(51,self.s.get_group_mood(2)['mood'])
        self.assertIsNone(self.s.get_group_mood(3))
        self.s.apply_interaction(1,'g:2','m2','刚才是引用别人的台词',dict(kind='correction',evidence='刚才是引用别人的台词',confidence=.99,target='bot',prior_id=event),self.now)
        self.assertEqual(50,self.s.get_group_mood(2)['mood'])


if __name__=='__main__': unittest.main()
