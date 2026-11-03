import os,sys,tempfile,time,unittest
sys.path.insert(0,os.environ.get('BOT_TEST_PATH') or os.path.abspath(os.path.join(os.path.dirname(__file__),'..', '..', 'runtime', 'core')))
from store import Store
from flow_memory import MemoryMixin
import memory_use

class Bot(MemoryMixin):
    cfg={}
    def _canonical_name(self,uid,name): return name
    def _qzone_memory_section(self): return ''

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.getcwd()); self.s=Store(os.path.join(self.tmp.name,'bot.db'))
        self.b=Bot(); self.b.store=self.s
    def tearDown(self): self.s.conn.close(); self.tmp.cleanup()
    def test_ordinary_old_fact_not_unsolicited_but_relevant_old_event_available(self):
        a=self.s.add_person_memory(1,'小甲两年前买了一支廉价铅笔','event',3)
        self.s.conn.execute('UPDATE person_memories SET created=?',(int(time.time())-300*86400,)); self.s.conn.commit()
        self.assertNotIn('廉价铅笔',self.b.build_memory_block(1,'小甲',False,0,'你好'))
        self.assertIn('廉价铅笔',self.b.build_memory_block(1,'小甲',False,0,'廉价铅笔'))
    def test_background_cooldown_does_not_block_explicit_recall(self):
        self.s.add_person_memory(1,'小甲正在准备明天的数学考试','event',3)
        self.assertIn('数学考试',self.b.build_memory_block(1,'小甲',False,0,'你好'))
        self.assertNotIn('数学考试',self.b.build_memory_block(1,'小甲',False,0,'你好'))
        self.assertIn('数学考试',self.b.build_memory_block(1,'小甲',False,0,'数学考试'))
    def test_only_reply_text_match_records_mention(self):
        a=self.s.add_person_memory(1,'小甲喜欢星空探索游戏','preference',4)
        candidates=[('person_memories',a['id'],'小甲喜欢星空探索游戏')]
        memory_use.finish(self.s,candidates,'p:1','m1','今天吃什么')
        self.assertEqual([],self.s.memory_usage_rows())
        memory_use.finish(self.s,candidates,'p:1','m2','你不是喜欢星空探索游戏吗')
        self.assertEqual(1,len(self.s.memory_usage_rows()))
    def test_high_facts_survive_capacity_without_manual_protection(self):
        for i in range(30): self.s.add_person_memory(1,'第%04d项资料必须长期保留'%i,'fact',4)
        result = self.s.cap_high_tier(5)
        self.assertEqual(0, result['demoted'])
        self.s._prune_scope('person_memories','qq',1,5)
        self.assertEqual(30,len(self.s.list_person_memories(1,100)))

    def test_latest_correction_reaches_reply_context(self):
        a=self.s.apply_interaction(1,'p:1','m1','成绩过了',dict(kind='good_news',target='self',confidence=.99,evidence='成绩过了',topic='数学考试',state='completed'))
        self.s.apply_interaction(1,'p:1','m2','其实没过，我看错了',dict(kind='correction',target='self',confidence=.99,evidence='其实没过，我看错了',topic='数学考试',state='corrected',prior_id=a))
        block=self.b.build_relation_block(1,'小甲',False,0)
        self.assertIn('其实没过，我看错了',block)
        self.assertNotIn('当前原话：成绩过了',block)

if __name__=='__main__': unittest.main()
