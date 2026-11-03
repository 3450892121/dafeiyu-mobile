# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'runtime', 'core')))
import recent_topic
from store import Store


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.store = Store(os.path.join(self.tmp.name, "bot.db"))

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_recent_is_scoped_expires_and_clear_context_invalidates(self):
        now = int(time.time())
        for i in range(4):
            self.store.add_message("p:1", 1, "小甲", "in", "私聊正在修电脑%d" % i, ts=now)
            self.store.add_message("g:7", 2, "小乙", "in", "群里正在讨论游戏%d" % i, ts=now)
        captured = []
        pool = types.SimpleNamespace(summarize_recent_topic=lambda text: captured.append(text) or "正在修电脑")
        bot = types.SimpleNamespace(store=self.store, pool=pool, summarizing=set(),
                                    summarizing_lock=threading.Lock(), _self_qq=lambda: 3,
                                    _bot_names=lambda: [])
        original = recent_topic.threading.Thread
        class Immediate:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
        try:
            recent_topic.threading.Thread = Immediate
            recent_topic.schedule(bot, "p:1")
        finally:
            recent_topic.threading.Thread = original
        self.assertIn("修电脑", captured[0])
        self.assertNotIn("讨论游戏", captured[0])
        self.assertEqual("正在修电脑", recent_topic.read(self.store, "p:1", int(time.time())))
        self.assertEqual("", recent_topic.read(self.store, "g:7", now))
        self.assertEqual("", recent_topic.read(self.store, "p:1", now + recent_topic.WINDOW + 1))
        self.store.reset_session("p:1")
        self.assertEqual("", recent_topic.read(self.store, "p:1", now))
        recent_topic.schedule(bot, "p:1")
        self.assertEqual("", recent_topic.read(self.store, "p:1", now))
        self.assertEqual(1, len(captured))
        self.store.set_setting("ctx_start:p:1", "1000")
        self.assertEqual("", recent_topic.read(self.store, "p:1", now))
        try:
            recent_topic.threading.Thread = Immediate
            recent_topic.schedule(bot, "g:7")
        finally:
            recent_topic.threading.Thread = original
        self.assertIn("小乙", captured[-1])
        self.assertIn("讨论游戏", captured[-1])
        self.assertNotIn("私聊正在修电脑", captured[-1])

    def test_recent_topic_does_not_reintroduce_old_turns(self):
        now = int(time.time())
        self.store.add_message("p:5", 5, "小甲", "in", "很早以前的问题", ts=now)
        for i in range(12):
            self.store.add_message("p:5", 5, "小甲", "in", "新话题%d" % i, ts=now)
        captured = []
        pool = types.SimpleNamespace(summarize_recent_topic=lambda text: captured.append(text) or "新话题")
        bot = types.SimpleNamespace(store=self.store, pool=pool, summarizing=set(),
                                    summarizing_lock=threading.Lock(), _self_qq=lambda: 0,
                                    _bot_names=lambda: [])
        original = recent_topic.threading.Thread
        class Immediate:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
        try:
            recent_topic.threading.Thread = Immediate
            recent_topic.schedule(bot, "p:5")
        finally:
            recent_topic.threading.Thread = original
        self.assertNotIn("很早以前的问题", captured[0])
        self.assertIn("新话题11", captured[0])


if __name__ == "__main__":
    unittest.main()
