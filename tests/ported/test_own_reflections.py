import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
from store import Store
from flow_memory import MemoryMixin
from flow_media import MediaMixin
import tools_gate


class OwnReflectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / 'bot.db'))
        self.store.self_bootstrap([])
        class Bot(MemoryMixin, MediaMixin):
            pass
        self.bot = Bot(); self.bot.store = self.store; self.bot.cfg = {}
        tools_gate._READY['v'] = False

    def tearDown(self):
        self.store.conn.close(); self.tmp.cleanup()

    def test_read_actual_diary_and_hide_other_conversation_details(self):
        self.store.upsert_diary('__day__', '2026-10-30', '我喜欢轻柔的器乐。某人告诉我他的私事是虚构蓝箱密码。')
        self.store.upsert_diary('p:101', '2026-10-31', '在私聊里谈了虚构紫箱地址。')
        result = self.bot.read_diary_tool({'day': '2026-10-30'}, 'g:202')
        self.assertIn('我喜欢轻柔的器乐', result)
        self.assertNotIn('蓝箱', result)
        self.assertIn('当时的回顾', result)
        result = self.bot.read_diary_tool({'day': '2026-10-31'}, 'g:202')
        self.assertNotIn('紫箱', result)
        self.assertIn('写过日记', result)
        self.assertIn('紫箱', self.bot.read_diary_tool({'day': '2026-10-31'}, 'p:101'))
        self.assertNotIn('紫箱', self.bot.read_diary_tool({'query': '紫箱'}, 'g:202'))

    def test_missing_or_invalid_diary_never_returns_another_day(self):
        self.store.upsert_diary('__day__', '2026-10-30', '我喜欢木管的音色。')
        self.assertIn('没有找到', self.bot.read_diary_tool({'day': '2026-10-29'}, 'g:202'))
        self.assertIn('没有读取', self.bot.read_diary_tool({'day': '2026-02-30'}, 'g:202'))

    def test_notebook_auto_context_and_search_use_same_privacy_boundary(self):
        self.store.add_notebook('global', '虚构紫箱私事内容', source_conv='p:101', private=1)
        self.store.add_notebook('global', '本群约好比较两段木管演奏', source_conv='g:202', private=0)
        self.store.add_notebook('global', '我喜欢柔和的木管音色', source_conv='p:303', private=0)
        result = self.bot.build_notebook_block(True, 202, 101, True)
        self.assertNotIn('紫箱', result)
        self.assertIn('本群约好', result)
        self.assertIn('木管音色', result)
        self.assertIn('紫箱', self.bot.build_notebook_block(False, 0, 101, True))
        with patch('flow_media.blocked', return_value=False), patch('flow_media.policy_changed', return_value=False):
            result = self.bot._exec_extra_tool('notebook_list', {}, 'g:202', True, 202, 101, True, '虚构管理员')
        self.assertNotIn('紫箱', result)

    def test_guessed_private_notebook_id_cannot_be_deleted_from_group(self):
        item = self.store.add_notebook('global', '虚构秘密笔记', source_conv='p:101', private=1)
        with patch('flow_media.blocked', return_value=False), patch('flow_media.policy_changed', return_value=False):
            result = self.bot._exec_extra_tool('notebook_delete', {'id': item['id']}, 'g:202', True, 202, 101, True, '虚构管理员')
        self.assertIn('不能凭编号删除', result)
        self.assertTrue(self.store.list_notebook('global'))

    def test_diary_tool_is_offered_to_both_scopes_without_owner_privileges(self):
        for group in (True, False):
            names = {x['function']['name'] for x in self.bot._tool_defs(False, is_group=group)}
            self.assertIn('read_diary', names)
            self.assertNotIn('message_person', names)


if __name__ == '__main__':
    unittest.main()
