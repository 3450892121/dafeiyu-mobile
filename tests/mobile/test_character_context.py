"""Identity metadata follows the current character; real quoted speech stays intact."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from blacklist import handle_command
from flow_memory import MemoryMixin, build_memory_prompt, card_role_reversed
import reply_autonomy
from store import Store
from thread_view import build_thread_block


class CharacterContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(str(Path(self.temp.name)/'bot.db'));self.addCleanup(self.store.conn.close)
        self.cfg={'bot_name':'纳西妲','trigger_name':'小妲','self_growth_enabled':False,'owner_qq':20}

    def test_thread_identity_uses_live_configuration_and_preserves_other_characters_speech(self):
        original='大肥鱼原话：本鲸今天想读书。'
        rows=[dict(direction='out',text='我刚才说的。',sender_id=99,message_id=1,ts=100),
              dict(direction='in',text=original,sender_id=101,sender_name='大肥鱼',message_id=2,ts=101),
              dict(direction='in',text='我是群友。',sender_id=102,sender_name='小妲',message_id=3,ts=102)]
        store=SimpleNamespace(thread_rows=lambda *args:rows)
        identity=SimpleNamespace(self_id=99,cfg=self.cfg,bot_name=Mock(side_effect=ValueError('synthetic')),
                                 name=lambda uid,name:name,mention_names=lambda segs:[])
        result=build_thread_block(store,identity,'g:202',101,'大肥鱼','看看前面聊天',trigger_name='小妲')
        self.assertIn('你（纳西妲）',result)
        self.assertIn('小妲(102)（同名号，不是你）',result)
        self.assertNotIn('大肥鱼(101)（同名号，不是你）',result)
        self.assertIn(original,result)
        self.assertEqual(rows[1]['text'],original)
        self.cfg['bot_name']='测试角色乙'
        result=build_thread_block(store,identity,'g:202',101,'大肥鱼','看看前面聊天',trigger_name='小乙')
        self.assertIn('你（测试角色乙）',result)

    def test_missing_thread_identity_has_a_neutral_fallback(self):
        store=SimpleNamespace(thread_rows=lambda *args:[dict(direction='out',text='在呢',sender_id=99,message_id=1)])
        result=build_thread_block(store,None,'g:202',101,'好友','你好')
        self.assertIn('你（机器人）',result)
        for name in ('纳西妲','大肥鱼','本鲸'):self.assertNotIn(name,result)

    def test_memory_variants_and_same_name_markers_use_current_names(self):
        bot=MemoryMixin();bot.cfg=self.cfg;bot.store=self.store
        bot._self_qq=lambda:99
        bot._bot_name=lambda:self.cfg['bot_name']
        bot._bot_names=lambda:[self.cfg['bot_name'],self.cfg['trigger_name']]
        bot._group_speak_bot_ids=lambda:set()
        bot._canonical_name=lambda uid,name:name
        bot._identity_corrections=lambda:[]
        bot._find_duplicate=Mock(return_value=(None,0))
        bot._merge_vec_duplicates=Mock()
        rows=[dict(id=1,direction='in',sender_id=101,sender_name='小妲',text='我喜欢阅读古籍，周六一起读书。'),
              dict(id=2,direction='in',sender_id=202,sender_name='大肥鱼',text='约好周六一起阅读古籍。')]
        items=[dict(who='group',kind='commitment',importance=5,text='小妲与大肥鱼约好周六一起阅读古籍',evidence=[1,2]),
               dict(who='101',kind='preference',importance=4,text='小妲一直喜欢阅读古籍',evidence=[1])]
        shared=[dict(text='小妲与大肥鱼约好周六阅读古籍',importance=4,people=['101','202'])]
        bot.pool=SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True,text=json.dumps(dict(items=items,shared=shared),ensure_ascii=False))))
        with patch('flow_memory.embed_texts',return_value=[]):
            self.assertEqual(bot._apply_memory_batch('g:303',{'gid':303,'is_group':True},rows,allow_share=False,update_card=False),3)
        calls=bot._find_duplicate.call_args_list
        self.assertEqual(len(calls),3)
        self.assertEqual(calls[0].kwargs['variants'],['纳西妲','小妲','303'])
        for call in calls:
            self.assertIn('纳西妲',call.kwargs['variants'])
            self.assertIn('小妲',call.kwargs['variants'])
            self.assertNotIn('蓝色大肥鱼',call.kwargs['variants'])
        prompt=bot.pool.chat.call_args.args[2]
        self.assertIn('小妲（同名号，不是你）(QQ101)',prompt)
        self.assertNotIn('大肥鱼（同名号，不是你）(QQ202)',prompt)
        self.assertIn(rows[1]['text'],prompt)

    def test_memory_instructions_and_reversed_card_check_do_not_prescribe_another_identity(self):
        source='朋友原话：本鲸今天聊大肥鱼。'
        prompt=build_memory_prompt('你是纳西妲。','好友(QQ101)',source)
        self.assertIn(source,prompt)
        self.assertNotIn('另一个大肥鱼',prompt)
        self.assertTrue(card_role_reversed('对方是纳西妲，一个QQ机器人。',['纳西妲','小妲']))
        self.assertFalse(card_role_reversed('对方是大肥鱼，喜欢阅读古籍。',['纳西妲','小妲']))

    def test_blacklist_self_protection_response_is_neutral(self):
        bot=SimpleNamespace(cfg=self.cfg,self_id=99,store=self.store,_send=Mock(return_value=True))
        self.assertTrue(handle_command(bot,{'message':[{'type':'text','data':{'text':'拉黑 99'}}]},20,0,False))
        text=bot._send.call_args.args[3][0]['data']['text']
        self.assertIn('当前机器人',text)
        self.assertNotIn('大肥鱼',text)
        self.assertEqual(self.store.blacklist_users(),[])

    def test_autonomy_missing_character_name_is_neutral_and_keeps_real_history(self):
        original='本鲸今天提到大肥鱼。'
        self.store.add_message('g:303',99,'旧显示名','out',original,message_id=10)
        bot=SimpleNamespace(cfg={'reply_autonomy_enabled':True,'self_growth_enabled':False},store=self.store,self_id=99,
                            persona=SimpleNamespace(owner='当前资料',unified_self=False),
                            pool=SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=False,text=''))))
        reply_autonomy.choose(bot,'g:303',101,'好友',False,True,'你好',{})
        payload=json.loads(bot.pool.chat.call_args.args[2])
        self.assertEqual(payload['机器人'],'机器人')
        self.assertIn(original,str(payload['近期会话（只作背景）']))


if __name__=='__main__':unittest.main()
