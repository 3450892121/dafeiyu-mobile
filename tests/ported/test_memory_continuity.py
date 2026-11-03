# -*- coding: utf-8 -*-
"""Offline checks for event updates and the private-memory boundary."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'runtime', 'core')))
from flow_memory import MemoryMixin
from store import Store
from model import ModelPool


class Bot(MemoryMixin):
    cfg = {}

    def _self_qq(self): return 456
    def _bot_name(self): return "大肥鱼"
    def _canonical_name(self, uid, name): return name
    def _identity_corrections(self): return []
    def _group_speak_bot_ids(self): return set()
    def _qzone_memory_section(self): return ""


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.store = Store(os.path.join(self.tmp.name, "bot.db"))
        self.bot = Bot()
        self.bot.store = self.store

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_private_fallback_and_dedup(self):
        self.store.add_person_memory(123, "小陈考试压力很大，私下失眠", "event", 3, "p:123", "private")
        self.store.add_person_memory(123, "小陈考试压力很大，已经通过", "event", 3, "g:1", "public")
        self.assertEqual(2, len(self.store.list_person_memories(123, 20)))
        group = self.bot.build_memory_block(123, "小陈", True, 1, "无关的话题")
        self.assertIn("已经通过", group)
        self.assertNotIn("私下失眠", group)
        self.assertIn("私下失眠", self.bot.build_memory_block(123, "小陈", False, 0, "无关的话题"))

    def test_event_update_preserves_old_and_clears_vector(self):
        old = self.store.add_person_memory(123, "小陈担心考试发挥不好", "event", 3, "p:123", "public", key_facts=["及格"])
        self.store.conn.execute("UPDATE person_memories SET created=? WHERE id=?", (int(time.time()) - 30 * 86400, old["id"]))
        self.store.conn.commit()
        self.store.set_memory_vec("person_memories", old["id"], [1.0, 0.0])
        item = {"who": "123", "evidence": [1], "kind": "event", "importance": 3, "text": "小陈考试成绩出来，已经通过",
                "merge": "P%d" % old["id"], "private": False}
        class Pool:
            def chat(self, *args, **kwargs):
                class Result:
                    ok = True
                    text = json.dumps({"items": [item], "shared": []}, ensure_ascii=False)
                return Result()
        self.bot.pool = Pool()
        rows = [{"id": 1, "direction": "in", "sender_id": 123, "sender_name": "小陈", "text": "考试过了"},
                {"id": 2, "direction": "out", "text": "太好了"}]
        self.assertEqual(1, self.bot._apply_memory_batch("p:123", {"uid": 123, "is_group": False}, rows,
                                                          allow_share=False, update_card=False))
        text = self.store.list_person_memories(123, 20)[0]["text"]
        self.assertIn("已经通过", text)
        self.assertIn("担心考试", text)
        self.assertNotIn("及格", self.store.list_person_memories(123, 20)[0]["key_facts"])
        block = self.bot.build_memory_block(123, "小陈", False, 0, "无关的话题")
        self.assertIn("已经通过", block)
        self.assertIn("更新于", block)
        self.assertIsNone(self.store.conn.execute("SELECT vec FROM person_memories WHERE id=?", (old["id"],)).fetchone()[0])
        item["text"] = "小陈纠正：成绩看错了，其实没过"
        self.assertEqual(1, self.bot._apply_memory_batch("p:123", {"uid": 123, "is_group": False}, rows,
                                                          allow_share=False, update_card=False))
        text = self.store.list_person_memories(123, 20)[0]["text"]
        self.assertLess(text.index("其实没过"), text.index("已经通过"))
        self.assertIn("担心考试", text)

    def test_archive_hints_and_group_dig_respect_scope(self):
        for address, table, owner, gist in [
            ("cold://p1", "person_memories", "123", "考试私事失眠"),
            ("cold://p2", "person_memories", "999", "考试别人私事"),
            ("cold://g3", "group_memories", "1", "考试群里公开话题"),
            ("cold://g4", "group_memories", "2", "考试其他群话题"),
        ]:
            self.store.conn.execute("INSERT INTO memory_archive(address,src_table,owner,text,gist) VALUES(?,?,?,?,?)",
                                    (address, table, owner, gist, gist))
        self.store.conn.commit()
        group = self.bot.build_memory_block(123, "小陈", True, 1, "考试")
        self.assertIn("考试群里公开话题", group)
        self.assertNotIn("私事失眠", group)
        self.assertNotIn("其他群话题", group)
        self.assertNotIn("私事失眠", self.bot.memory_dig_tool("cold://p1", True, 1, 123, False))
        private = self.bot.build_memory_block(123, "小陈", False, 0, "考试")
        self.assertIn("考试私事失眠", private)
        self.assertNotIn("别人私事", private)

    def test_summary_requests_current_state_and_corrections(self):
        captured = {}
        class FakePool:
            def chat(self, system, history, prompt, **kwargs):
                captured["prompt"] = prompt
                class Result:
                    ok = True
                    text = "测试摘要"
                return Result()
        self.assertEqual("测试摘要", ModelPool.summarize(FakePool(), "小陈：成绩看错了"))
        self.assertIn("最新进展和纠正", captured["prompt"])
        self.assertIn("话题已经转开", captured["prompt"])
        self.assertIn("活动类型不要猜", captured["prompt"])
        self.assertEqual("测试摘要", ModelPool.summarize_recent_topic(FakePool(), "示例"))
        self.assertIn("没人回答的闲聊提问不是未完成事项", captured["prompt"])

    def test_group_model_cannot_merge_into_another_person(self):
        a = self.store.add_person_memory(111, "小甲准备参加期末考试", "event", 3, "g:1", "public")
        item = {"who": "222", "evidence": [2], "kind": "event", "importance": 3,
                "text": "小乙期末考试已经通过", "merge": "P%d" % a["id"]}
        class Pool:
            def chat(self, *args, **kwargs):
                class Result:
                    ok = True
                    text = json.dumps({"items": [item], "shared": []}, ensure_ascii=False)
                return Result()
        self.bot.pool = Pool()
        rows = [{"id": 1, "direction": "in", "sender_id": 111, "sender_name": "小甲", "text": "我等成绩"},
                {"id": 2, "direction": "in", "sender_id": 222, "sender_name": "小乙", "text": "我的通过了"}]
        self.bot._apply_memory_batch("g:1", {"gid": 1, "is_group": True}, rows,
                                     allow_share=False, update_card=False)
        self.assertNotIn("小乙", self.store.list_person_memories(111, 20)[0]["text"])
        self.assertIn("小乙", self.store.list_person_memories(222, 20)[0]["text"])


if __name__ == "__main__":
    unittest.main()
