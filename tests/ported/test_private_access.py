import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
from main import Bot
from config import Config, DEFAULTS


class PrivateAccessTests(unittest.TestCase):
    def bot(self, all_users=False):
        bot = Bot.__new__(Bot)
        bot.cfg = {"owner_qq": 10, "allow_users": [20], "allow_groups": [40],
                   "allow_all_users": all_users, "group_need_at": True}
        bot.self_id = 99
        bot.store = SimpleNamespace(add_log=Mock(), has_message_id=lambda mid: False)
        bot._mark_seen = lambda mid: True
        bot._parse = lambda segs: ("你好", [], False, [], [])
        bot._bot_voice_note_text = lambda text, *args: text
        bot._reply_id = lambda segs: None
        bot._quote_info = lambda *args: None
        bot._forward_from_segs = lambda segs: ""
        bot._remember_sender = Mock()
        bot._reply = Mock()
        from contextlib import nullcontext
        bot._conv_lock = lambda conv: nullcontext()
        bot._note_latency = Mock()
        return bot

    def event(self, uid=30, **kw):
        return {"post_type": "message", "message_type": "private", "self_id": 99,
                "user_id": uid, "message_id": 123, "sender": {"nickname": "非好友"},
                "sub_type": "group", "message": [{"type": "text", "data": {"text": "你好"}}], **kw}

    def test_default_and_bool_normalization(self):
        self.assertFalse(DEFAULTS["allow_all_users"])
        cfg = Config.__new__(Config)
        for raw, expected in [("false", False), ("true", True), ("off", False), ("1", True)]:
            data = {"allow_all_users": raw}
            cfg._validate(data)
            self.assertIs(data["allow_all_users"], expected)

    def test_gate_batch_and_actual_nonfriend_reply_agree(self):
        for all_users in (False, True):
            for uid in (10, 20, 30):
                with self.subTest(all_users=all_users, uid=uid):
                    bot = self.bot(all_users)
                    expected = all_users or uid in (10, 20)
                    self.assertEqual(bot.explain_gate(False, 0, uid, "小甲", "你好")["reply"], expected)
                    self.assertEqual(bot._should_batch(self.event(uid)), expected)
                    bot.handle(self.event(uid))
                    self.assertEqual(bot._reply.called, expected)

    def test_invalid_sender_self_and_pause_still_blocked(self):
        bot = self.bot(True)
        self.assertFalse(bot._private_allowed(0))
        self.assertFalse(bot._should_batch(self.event(99)))
        bot.cfg["bot_paused"] = True
        self.assertFalse(bot.explain_gate(False, 0, 30, "", "你好")["reply"])
        bot.handle(self.event())
        bot._reply.assert_not_called()

    def test_group_allowlist_is_unchanged(self):
        bot = self.bot(True)
        result = bot.explain_gate(True, 50, 30, "", "你好")
        self.assertFalse(result["reply"])
        self.assertEqual(result["steps"][-1]["label"], "群在白名单里")

    def test_console_saves_toggle_without_changing_whitelists(self):
        spec = importlib.util.spec_from_file_location("private_test_console", Path(__file__).resolve().parents[2] / 'runtime/console_base.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cfg = SimpleNamespace(data={"allow_users": [20], "allow_groups": [40]})
        cfg.set = lambda k, v: cfg.data.__setitem__(k, v)
        cfg.get = lambda k, default=None: cfg.data.get(k, default)
        for raw, expected in [("true", True), ("0", False), ("1", True)]:
            module.apply_settings(cfg, {"allow_all_users": raw})
            self.assertIs(cfg.data["allow_all_users"], expected)
            self.assertEqual(cfg.data["allow_users"], [20])
            self.assertEqual(cfg.data["allow_groups"], [40])


if __name__ == "__main__":
    unittest.main()
