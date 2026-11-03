import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
from thread_view import select_image_references,build_thread_block,focus_history
from flow_context import ContextMixin

def row(mid,uid,text='[图片]',name=None,quote=None,at=0):
 return dict(message_id=mid,sender_id=uid,sender_name=name or ('虚构甲' if uid==101 else '虚构乙'),text=text,ts=100+mid,
             direction='in',attachments=[dict(type='quote',id=quote,who_qq=101,text='被引用的题目')] if quote else [],
             raw=json.dumps(dict(message=[dict(type='at',data=dict(qq=str(at)))] if at else [])))

class ReferenceTests(unittest.TestCase):
 def test_text_view_hides_auto_image_notes_but_preserves_source_and_explicit_image_questions(self):
  history=[dict(role='user',content='[甲(QQ101)] 额度太少'),dict(role='user',content='[乙(QQ303)] [表情包]（这位说话人发的图：红花和绿叶）')]
  for text in ('就这点额度，真无语了','看上面这句'):
   focused=focus_history(history,text)
   self.assertNotIn('红花',focused[1]['content'])
   self.assertIn('额度太少',focused[0]['content']);self.assertIn('QQ303',focused[1]['content'])
  self.assertIn('红花',history[1]['content'])
  self.assertIn('红花',focus_history(history,'这张图是什么花')[1]['content'])
  self.assertIn('红花',focus_history(history,'帮我看看',media_target=True)[1]['content'])
 def test_text_reference_does_not_promote_interleaved_sticker(self):
  image=row(2,101);timeline=[row(1,101,'额度太少任务跑不完'),image]
  for text in ('看上面这句','看看上面的聊天记录','看上面'):
   self.assertEqual([],select_image_references([image],timeline,text,101)[0])

 def test_multiple_people_images_require_a_target(self):
  images=[row(1,101),row(2,303)]
  selected,note=select_image_references(images,images,'看刚才那张图',101)
  self.assertEqual([],selected);self.assertIn('不唯一',note)
  self.assertEqual([1],[r['message_id'] for r in select_image_references(images,images,'我刚发的那张图',101)[0]])
  self.assertEqual([1],[r['message_id'] for r in select_image_references(images,images,'虚构甲刚才那张图',303)[0]])
  self.assertEqual([2],[r['message_id'] for r in select_image_references(images,images,'最新那张图',101)[0]])

 def test_two_images_keep_chronological_order_and_same_names_are_ambiguous(self):
  images=[row(1,101),row(2,303)]
  self.assertEqual([1,2],[r['message_id'] for r in select_image_references(images,images,'这两张图哪个更好',101)[0]])
  images[1]['sender_name']=images[0]['sender_name']
  self.assertEqual([],select_image_references(images,images,'虚构甲的那张图',101)[0])
  self.assertEqual([],select_image_references(images,images,'你刚发的那张图',101)[0])

 def test_reference_chain_does_not_merge_an_interleaved_conversation(self):
  rows=[row(1,101,'这道题想请你解释',at=99001),row(2,303,'明天去打球',at=404),row(3,505,'我也卡在这步',quote=1)]
  store=SimpleNamespace(thread_rows=lambda *a:rows)
  ident=SimpleNamespace(self_id=99001,bot_name=lambda:'鲸',name=lambda q,n:n,mention_names=lambda *a:[])
  text=build_thread_block(store,ident,'g:202',101,'甲','帮我看这步',quote=dict(id=1,text='这道题'))
  self.assertIn('【旁支，不自动接续】',text)
  self.assertEqual(2,text.count('【当前引用链】'))
  vague=build_thread_block(store,ident,'g:202',707,'丙','鲸',trigger_name='鲸')
  self.assertNotIn('当前这个人最近明确对你说过的',vague)

 def test_failed_image_read_does_not_relabel_next_image_as_first_author(self):
  bot=ContextMixin();bot.cfg={}
  bot.onebot=SimpleNamespace(get_msg=lambda mid:dict(message=mid))
  bot._parse=lambda mid:('',[dict(id=mid)],[],[],[])
  bot._image_data_urls=lambda segs,**kw:[] if segs[0]['id']==1 else ['fixture-image']
  urls,sources=bot._recent_image_urls('g:202',selected_rows=[row(1,101),row(2,303)],with_sources=True)
  self.assertEqual(['fixture-image'],urls)
  self.assertIn('QQ303',sources[0]);self.assertNotIn('QQ101',sources[0])

if __name__=='__main__':unittest.main()
