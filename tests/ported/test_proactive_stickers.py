import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.environ.get("BOT_TEST_PATH") or os.path.join(os.path.dirname(__file__), '..', '..', 'runtime', 'core'))
import flow_proactive as fp
import sticker_engine


class ProactiveStickerTests(unittest.TestCase):
    def bot(self, **cfg):
        bot = fp.ProactiveMixin()
        bot.cfg = {"sticker_judge_enabled": True, "sticker_wait_ready": True,
                   "owner_qq": 10, "allow_users": [20], **cfg}
        bot.self_id = 99
        settings = {}
        bot.store = SimpleNamespace(get_setting=lambda key: settings.get(key),
                                    set_setting=lambda key, value: settings.__setitem__(key, value),
                                    add_message=Mock(), touch_session=Mock(), add_log=Mock())
        bot.stickers = ["test.png"]
        bot._proactive_generate = Mock(return_value="刚整理到一张傻乐图，忍不住想给你看看。")
        bot._guard_owner_address = lambda text, *args: text
        bot._outbound_keys = lambda: []
        bot._text_segs = lambda text: [text]
        bot._send = Mock(return_value=True)
        bot._judge_sticker = Mock(return_value={"file": "test.png", "desc": "笑脸"})
        bot.send_sticker_item = Mock(return_value=True)
        bot._proactive_mark_global = Mock()
        bot._proactive_now = lambda: 100000
        return bot

    def send(self, bot):
        return bot._proactive_send({"conv": "p:20", "uid": 20, "nick": "小甲",
                                    "is_owner": False}, "2026-10-31", 30)

    def test_active_text_and_image_are_one_counted_turn_with_attachment(self):
        bot = self.bot()
        result = self.send(bot)
        self.assertTrue(result["ok"] and result["sticker"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(bot._judge_sticker.call_args.args[4], "")
        self.assertEqual(bot.store.add_message.call_args.args[-1],
                         [{"type": "sticker", "file": "test.png", "note": "笑脸"}])
        bot.send_sticker_item.assert_called_once()

    def test_both_wait_modes_keep_configured_send_order(self):
        for wait in (True, False):
            with self.subTest(wait=wait):
                bot = self.bot(sticker_wait_ready=wait)
                calls = []
                bot._judge_sticker.side_effect = lambda *a: calls.append("judge") or {"file": "test.png"}
                bot._send.side_effect = lambda *a: calls.append("text") or True
                bot.send_sticker_item.side_effect = lambda *a: calls.append("image") or True
                self.send(bot)
                self.assertEqual(calls, ["judge", "text", "image"] if wait else ["text", "judge", "image"])

    def test_disabled_or_empty_library_never_calls_judge(self):
        for disabled in (True, False):
            bot = self.bot(sticker_judge_enabled=not disabled)
            if not disabled:
                bot.stickers = []
            result = self.send(bot)
            self.assertTrue(result["ok"])
            self.assertFalse(result["sticker"])
            bot._judge_sticker.assert_not_called()
            bot.send_sticker_item.assert_not_called()

    def test_judgement_rejection_or_error_keeps_text_success(self):
        for error in (False, True):
            bot = self.bot()
            bot._judge_sticker.return_value = None
            if error:
                bot._judge_sticker.side_effect = RuntimeError("judge unavailable")
            result = self.send(bot)
            self.assertTrue(result["ok"])
            self.assertFalse(result["sticker"])
            self.assertEqual(bot.store.add_message.call_args.args[-1], [])

    def test_image_failure_does_not_claim_image_or_retry_text(self):
        for error in (False, True):
            bot = self.bot()
            bot.send_sticker_item.return_value = False
            if error:
                bot.send_sticker_item.side_effect = RuntimeError("send failed")
            result = self.send(bot)
            self.assertTrue(result["ok"])
            self.assertFalse(result["sticker"])
            bot._send.assert_called_once()
            self.assertEqual(bot.store.add_message.call_args.args[-1], [])

    def test_failed_text_never_sends_image_or_counts_turn(self):
        bot = self.bot()
        bot._send.return_value = False
        self.assertFalse(self.send(bot)["ok"])
        bot.send_sticker_item.assert_not_called()
        bot.store.add_message.assert_not_called()
        self.assertEqual(bot._proactive_count("p:20", "2026-10-31"), 0)

    def test_open_private_replies_never_expand_proactive_candidates(self):
        bot = self.bot(allow_all_users=True, proactive_all_friends=True, proactive_whitelist_only=False)
        bot.onebot = SimpleNamespace(get_friend_list=lambda: [{"user_id": 30, "nickname": "陌生人"}])
        bot.store.conv_names = lambda: {"p:30": "陌生人"}
        bot.store.people_overview = lambda *args: [{"qq": 30, "last_seen": 100000}]
        bot.store.get_session = lambda conv: {"last_active": 100000}
        bot.store.get_person_relation_effective = lambda uid, *args: {}
        self.assertEqual({x["uid"] for x in bot._friend_candidates()}, {10, 20})

    def test_brief_does_not_fabricate_new_inbound_message(self):
        bot = self.bot()
        bot.store.messages_for_context = lambda *a: [{"role": "assistant", "content": "昨晚的旧话"}]
        bot.pool = SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True, text='{"want": false}')))
        sticker_engine.brief(bot, "p:20", False, "", "我刚整理了新图")
        prompt = bot.pool.chat.call_args.args[2]
        self.assertIn("本轮没有新来话", prompt)
        self.assertNotIn("最后一条是对方刚说的", prompt)


if __name__ == "__main__":
    unittest.main()
