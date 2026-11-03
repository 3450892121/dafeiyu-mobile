import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from onebot import OneBot,text_seg
from store import Store
import weather


class LocalDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(str(Path(self.tmp.name)/'bot.db'));self.addCleanup(self.store.conn.close)
    def test_reminders_preserve_target_and_do_not_repeat_after_confirmation(self):
        identifier=self.store.add_reminder('p:101',100,'喝水',target_uid=101,target_name='虚构甲')
        self.assertEqual(self.store.due_reminders(99),[])
        due=self.store.due_reminders(101);self.assertEqual(len(due),1);self.assertEqual(due[0]['target_uid'],101)
        self.store.mark_reminder(identifier);self.assertEqual(self.store.due_reminders(102),[])
    def test_notebook_and_knowledge_are_scope_isolated_and_searchable(self):
        self.store.add_notebook('p:101','临时备忘：周末看书',source_conv='p:101',source_uid=101,private=1)
        self.assertTrue(self.store.list_notebook('p:101'));self.assertFalse(self.store.list_notebook('g:202'))
        self.store.add_kb_doc('p:101','Python学习','Python用于处理资料，SQLite保存本机笔记。')
        self.assertTrue(self.store.search_kb('p:101',['Python']))
        self.assertFalse(self.store.search_kb('g:202',['Python']))
    def test_diary_upsert_and_database_integrity(self):
        self.store.upsert_diary('p:101','2026-11-03','今天认识了新朋友。')
        self.store.upsert_diary('p:101','2026-11-03','今天聊起了新计划。')
        diaries=self.store.list_diaries();self.assertEqual(len(diaries),1);self.assertIn('新计划',diaries[0]['text'])
        self.assertEqual(self.store.conn.execute('PRAGMA quick_check').fetchone()[0],'ok')
    def test_weather_does_not_inherit_reference_city(self):
        self.assertEqual(weather.HOME_CITY,'')
        with patch.object(weather,'fetch')as fetch:
            self.assertIsNone(weather.resolve('明天天气呢',conv='new-instance'))
            fetch.assert_not_called()
        self.assertEqual(weather.clean_city('明天北京天气怎么样'),'北京')


class TransportContractTests(unittest.TestCase):
    def test_real_http_adapter_emits_only_selected_local_authenticated_actions(self):
        calls=[]
        def response(request,timeout):
            calls.append((request.full_url,request.get_header('Authorization'),json.loads(request.data)))
            return io.BytesIO(json.dumps({'status':'ok','retcode':0,'data':{'tid':'fictional-post'}}).encode())
        client=OneBot('http://127.0.0.1:33111','fictional-only-token')
        with patch('onebot.urllib.request.urlopen',side_effect=response):
            client.send_private(101,[text_seg('测试')]);client.send_group(202,[text_seg('测试')])
            client.get_friend_msg_history(101,10);client.get_group_msg_history(202,10,message_seq=3)
            client.get_msg(55);client.get_image('fictional-image')
            client.upload_private_file(101,'/home/mobile/data/files/one.docx','one.docx')
            client.upload_group_file(202,'/home/mobile/data/files/two.xlsx','two.xlsx')
            self.assertEqual(client.send_qzone('虚构内容'),'fictional-post');client.delete_qzone('fictional-post');client.get_qzone_cookies()
        self.assertEqual(len(calls),11)
        self.assertTrue(all(url.startswith('http://127.0.0.1:33111/')and auth=='Bearer fictional-only-token'for url,auth,_ in calls))
        self.assertEqual(calls[0][2]['user_id'],101);self.assertEqual(calls[1][2]['group_id'],202)
        self.assertEqual(calls[3][2]['message_seq'],3);self.assertEqual(calls[-1][2]['domain'],'user.qzone.qq.com')

if __name__=='__main__':unittest.main()
