"""Autonomous openings keep one family relationship without inventing an input turn."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.environ.get("BOT_TEST_PATH") or str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
from store import Store
from persona import Persona
from flow_proactive import ProactiveMixin


class SelfGrowthProactiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.root = Path(self.tmp.name)
        self.store = Store(str(self.root / "data" / "bot.db"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.conn.close)
        assets = self.root / "appassets"
        assets.mkdir()
        (assets / "persona_owner.txt").write_text("最初曾被写成固定温柔口吻，叫对方主人。", encoding="utf-8")
        (assets / "persona_group.txt").write_text("另一份旧群聊写法，不是另一个身份。", encoding="utf-8")
        (assets / "persona_identity.txt").write_text("蓝色大肥鱼，一个QQ机器人。", encoding="utf-8")
        a = self.store.add_message("p:101", 101, "甲", "in", "音乐你想自己选哪一类？", ts=1000)
        b = self.store.add_message("p:101", 99001, "鱼", "out", "音乐我想先选纯器乐，仔细听一下配器。", ts=1001)
        self.sources = self.store.growth_material("p:101", self.store.messages_after("p:101", 0, 20), 99001)
        self.plan = {"topic": "音乐", "kind": "interest", "subject": 0, "stance": "最近想自己挑音乐的配器来听。",
                     "reason": "一次实际交流中自己选择了纯器乐。", "source_ids": [a, b], "counter_ids": [], "target_id": 0}
        self.assertEqual(1, self.store.commit_growth_review("p:101", {"self_growth": [self.plan]}, self.sources)["added"])
        self.trait = self.store.growth_snapshot("p:101")["items"][0]

    def test_autonomous_context_is_not_a_fabricated_reply_pair(self):
        incoming = self.store.add_message("p:101", 101, "甲", "in", "今天音乐的话题先到这里。", ts=1010)
        outgoing = self.store.add_message("p:101", 99001, "鱼", "out", "音乐我自己想试试不同的配器。", ts=1020)
        self.assertTrue(self.store.growth_note_provided("p:101", outgoing, [self.trait["id"]]))
        self.assertFalse(self.store.growth_note_provided("p:101", 999999, [self.trait["id"]]))
        row = self.store.conn.execute("SELECT inbound_id FROM self_growth_turns WHERE outbound_id=?", (outgoing,)).fetchone()
        self.assertEqual(0, row[0])
        material = self.store.growth_material("p:101", self.store.messages_after("p:101", 0, 20), 99001)
        plan = dict(self.plan, source_ids=[incoming, outgoing], target_id=self.trait["id"])
        self.assertGreater(self.store.commit_growth_review("p:101", {"self_growth": [plan]}, material)["rejected"], 0)
        self.assertEqual(self.trait["version"], self.store.growth_snapshot("p:101")["items"][0]["version"])

    def test_proactive_generation_reads_visible_growth_and_keeps_family_when_disabled(self):
        # Use fresh timestamps for the temporary tendency's natural retrieval window.
        bot = ProactiveMixin()
        bot.cfg = {"self_growth_enabled": True, "owner_qq": 101, "bot_name": "蓝色大肥鱼"}
        bot.store = self.store
        bot.persona = Persona(str(self.root), cfg=bot.cfg)
        bot._proactive_now_line = lambda: "2026-11-02 16:00"
        bot._proactive_open_prompt = lambda *a: "结合音乐的话题，说一句自己的想法。"
        bot._proactive_recent_texts = lambda *a: []
        bot._proactive_text_seen = lambda *a: False
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                bot.cfg["self_growth_enabled"] = enabled
                bot.pool = SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True, text="音乐我想换段配器仔细听听。")))
                cand = {"conv": "p:101", "uid": 101, "is_owner": True, "nick": "甲"}
                self.assertTrue(bot._proactive_generate(cand, 30, {"material": "音乐"}))
                bot.pool.chat.assert_called_once()
                system = bot.pool.chat.call_args.args[0]
                self.assertIn("本实例管理员", system)
                self.assertIn("不继承其他实例已经发生的家庭或亲密关系", system)
                self.assertIn("同一个自己", system)
                # Pausing learning freezes updates; it does not remove the current self.
                self.assertIn(self.trait["stance"], system)
                self.assertEqual([self.trait["id"]], cand["_growth_provided_ids"])


if __name__ == "__main__":
    unittest.main()
