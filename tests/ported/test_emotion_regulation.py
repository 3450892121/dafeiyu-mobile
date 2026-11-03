import os,sys,tempfile,time,unittest
sys.path.insert(0,os.environ.get('BOT_TEST_PATH') or os.path.abspath(os.path.join(os.path.dirname(__file__),'..', '..', 'runtime', 'core')))
from store import Store
from flow_context import BotVoiceNoteMixin

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.getcwd());self.s=Store(self.tmp.name+'/bot.db');self.now=int(time.time());self.n=0
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()
    def event(self,kind,prior=0,conv='p:1'):
        self.n+=1
        return self.s.apply_interaction(1,conv,str(self.n),'当前真实原文',dict(kind=kind,evidence='当前真实原文',confidence=.99,target='bot',prior_id=prior),self.now)
    def moods(self):return {x['kind']:x['intensity'] for x in self.s.get_emotions(1,now=self.now)}
    def test_hurt_interrupts_happy_and_shy(self):
        self.s.add_emotion(1,'happy',40,now=self.now);self.s.add_emotion(1,'shy',20,now=self.now)
        self.event('betrayal');m=self.moods()
        self.assertEqual(15,m['happy']);self.assertEqual(2,m['shy']);self.assertEqual(20,m['upset'])
    def test_apology_calms_but_does_not_restore_trust(self):
        self.event('betrayal');self.event('apology')
        self.assertEqual(27,self.s.get_person_relation(1)['trust']);self.assertEqual(8,self.moods()['upset'])
        self.now+=2*86400
        self.assertLess(self.moods().get('upset',0),.5)
        self.assertEqual(27,self.s.effective_trust(27,self.now-2*86400,now=self.now))
        self.event('fulfilled');self.assertEqual(29,self.s.get_person_relation(1)['trust'])
    def test_repetition_diminishes_across_conversations_and_recovers(self):
        self.event('praise');self.event('praise',conv='g:3');self.event('praise',conv='g:4')
        rows=self.s.interaction_events(1);self.assertEqual([1.5,3,6],[r['effects']['moods']['happy'] for r in rows])
        self.now+=6*3600+1;self.event('praise');self.assertEqual(1,self.s.interaction_events(1)[0]['effects']['regulation']['repeat_factor'])
    def test_flattery_does_not_wash_away_hurt(self):
        self.s.add_emotion(1,'upset',60,now=self.now);self.event('affection')
        self.assertEqual(60,self.moods()['upset']);self.assertLess(self.moods().get('happy',0),1)
    def test_support_calms_worry_and_sadness(self):
        self.s.add_emotion(1,'worry',30,now=self.now);self.s.add_emotion(1,'sad',20,now=self.now)
        self.event('support');self.assertEqual(22,self.moods()['worry']);self.assertEqual(12,self.moods()['sad'])
    def test_correction_restores_frozen_trust_and_remaining_emotions_once(self):
        a=self.event('betrayal');self.now+=180*86400;self.event('correction',a)
        self.assertEqual(30,self.s.get_person_relation(1)['trust'])
        self.event('correction',a);self.assertEqual(30,self.s.get_person_relation(1)['trust'])
    def test_low_trust_stays_low_but_high_trust_relaxes(self):
        self.assertEqual(20,self.s.effective_trust(20,self.now,now=self.now+999*86400))
        self.assertEqual(60,self.s.effective_trust(90,self.now,now=self.now+180*86400))
    def test_every_emotion_halves_without_events(self):
        for k,h in self.s.EMOTION_HALF.items():
            self.s.add_emotion(1,k,20,now=self.now)
            vals={e['kind']:e['intensity'] for e in self.s.get_emotions(1,now=self.now+h)}
            self.assertEqual(10,vals[k])
    def test_self_quote_overrides_ambiguous_label(self):
        text=BotVoiceNoteMixin.format_quote_block(dict(id=7,is_bot=True,images=[{}],text='[表情包]',relation_text='别人发的'), '小甲',only_at=True)
        self.assertIn('你自己以前发出的',text);self.assertIn('不是原图发送者',text);self.assertNotIn('别人发的',text)
    def test_quote_sender_top_level_id_is_not_lost(self):
        import types
        class Bot(BotVoiceNoteMixin):
            def _self_qq(self):return 7
            def _person_name(self,q,n):return n or str(q)
            def _quote_preview(self,*a,**kw):return '[表情包]'
            def _forward_from_segs(self,*a):return ''
        b=Bot();b.onebot=types.SimpleNamespace(get_msg=lambda mid:dict(user_id=7,message=[dict(type='image',data=dict(url='https://example.invalid/image'))]))
        result=b._quote_info(99,8,'引用者')
        self.assertTrue(result['is_self']);self.assertEqual(7,result['who_qq'])
        self.assertIn('你自己',b.format_quote_block(result,'引用者'))

if __name__=='__main__':unittest.main()
