import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'runtime', 'core'))
import video_browse as vb


class VideoBrowseNotificationTests(unittest.TestCase):
    def setUp(self):
        self.contact = SimpleNamespace(allowed=True, check=Mock(return_value=True),
                                       sending=Mock(return_value=True), sent=Mock())
        self.attempt_patch = patch.object(vb.private_contact, "attempt")
        self.attempt = self.attempt_patch.start()
        self.attempt.return_value.__enter__.return_value = self.contact
        self.addCleanup(self.attempt_patch.stop)

    def bot(self, notify):
        bot = vb.VideoBrowsingMixin()
        bot.cfg = {"video_browse_notify": notify, "video_browse_enabled": True,
                   "owner_qq": 10, "video_browse_max_minutes_per_day": 60}
        settings = {}
        bot.store = SimpleNamespace(get_setting=lambda k: settings.get(k),
                                    set_setting=lambda k, v: settings.__setitem__(k, v))
        bot._vb_say = Mock()
        bot._vb_status_write = Mock()
        bot._vb_token_ok = Mock(return_value=True)
        bot._vb_token_used_today = Mock(return_value=100)
        bot._vb_token_limit = Mock(return_value=100)
        bot._vb_model_ids = Mock(return_value=[])
        bot._vb_token_add_usage = Mock()
        bot.persona = SimpleNamespace(system_prompt=Mock(return_value="test"))
        bot.pool = SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True, text="今天额度用完了。")))
        return bot

    def state(self, **kw):
        return {"source": "scheduled", "started": time.time(), "deadline": time.time() - 1,
                "minutes": 10, "stop": threading.Event(), "conv": "p:10",
                "is_group": False, "gid": 0, "uid": 10, "watched": 0, "titles": [], **kw}

    def test_scheduled_start_still_runs_without_notification(self):
        for notify in (False, True):
            bot = self.bot(notify)
            now = time.time()
            plan = [{"start": now - 1, "end": now + 1200, "minutes": 10}]
            bot._vb_get_plan = Mock(return_value=plan)
            bot._vb_save_plan = Mock()
            with patch.object(vb.threading, "Thread") as thread:
                self.assertTrue(bot._vb_try_scheduled(now))
                thread.return_value.start.assert_called_once()
            bot._vb_say.assert_not_called()
            self.assertEqual(bot._vb_used_minutes_today(), 10)
            self.assertTrue(plan[0]["started"])

    def test_finish_and_interruption_respect_live_notification_setting(self):
        for notify in (False, True):
            for reason in ({}, {"watched": 2, "titles": ["测试视频"]},
                           {"risk_stop": True}, {"mem_low": True}, {"stopped": True}):
                with self.subTest(notify=notify, reason=reason):
                    bot = self.bot(notify)
                    state = self.state(**reason)
                    if reason.get("stopped"):
                        state["stop"].set()
                    bot._vb_state = state
                    bot._vb_add_minutes(10)
                    bot._vb_run(state)
                    self.assertIsNone(bot._vb_state)
                    self.assertEqual(bot.store.get_setting(vb.RUNNING_KEY), "")
                    meaningful = bool(reason.get("watched") or reason.get("risk_stop") or reason.get("mem_low"))
                    self.assertEqual(bot._vb_say.call_count, int(notify and meaningful))
                    self.assertLess(bot._vb_used_minutes_today(), 10)
                    bot._vb_status_write.assert_called_once()

    def test_silent_budget_notice_avoids_model_and_send(self):
        bot = self.bot(False)
        self.assertFalse(bot._vb_token_notice("p:10", False, 0, 10))
        bot.pool.chat.assert_not_called()
        bot._vb_say.assert_not_called()

    def test_budget_exhausted_run_is_silent_and_cleans_up(self):
        bot = self.bot(False)
        bot._vb_token_ok.return_value = False
        bot._vb_state = self.state()
        bot._vb_run(bot._vb_state)
        bot.pool.chat.assert_not_called()
        bot._vb_say.assert_not_called()
        self.assertIsNone(bot._vb_state)
        bot._vb_status_write.assert_called_once()

    def test_explicit_budget_request_still_gets_answer(self):
        bot = self.bot(False)
        self.assertTrue(bot._vb_token_notice("p:10", False, 0, 10, force=True))
        bot.pool.chat.assert_called_once()
        bot._vb_say.assert_called_once()

    def test_explicit_progress_request_still_gets_answer(self):
        bot = self.bot(False)
        bot._vb_state = self.state()
        self.assertTrue(bot._video_browse_command("刷到哪了？", "p:10", False, 0, 10))
        bot._vb_say.assert_called_once()

    def test_unanswered_auto_video_and_notices_do_not_send(self):
        self.contact.allowed = False
        bot = self.bot(True)
        bot.self_id = 99
        bot._send = Mock(return_value=True)
        bot._vb_share_append = Mock()
        item = {"bvid": "BV1", "message": "这段视频让我想到你前两天聊的那道题，里面的这个做法我觉得还挺有用的。",
                "url": "https://www.bilibili.com/video/BV1"}
        self.assertFalse(bot._vb_share_send(item))
        self.assertFalse(bot._vb_notify("p:10", False, 0, 10, "刷完了，看了两条。"))
        bot._send.assert_not_called()
        bot._vb_say.assert_not_called()
        self.contact.sent.assert_not_called()
        self.assertEqual(bot._vb_share_count_today(), 0)

    def test_auto_video_rechecks_before_send_and_marks_only_success(self):
        bot = self.bot(True)
        bot.self_id = 99
        bot._send = Mock(return_value=True)
        bot._vb_share_append = Mock()
        bot.store.add_message = Mock()
        item = {"bvid": "BV1", "message": "这段视频让我想到你前两天聊的那道题，里面的这个做法我觉得还挺有用的。"}
        self.contact.check.return_value = False
        self.assertFalse(bot._vb_share_send(item))
        bot._send.assert_not_called()
        self.contact.check.return_value = True
        self.contact.sending.return_value = False
        self.assertFalse(bot._vb_share_send(item))
        bot._send.assert_not_called()
        self.contact.sending.return_value = True
        bot._send.return_value = False
        self.assertFalse(bot._vb_share_send(item))
        self.contact.sent.assert_not_called()
        bot._send.return_value = True
        self.assertTrue(bot._vb_share_send(item))
        self.contact.sent.assert_called_once()
        self.assertEqual(bot._vb_share_count_today(), 1)

    def test_disabled_video_shares_and_proactive_quota_really_zero(self):
        bot = self.bot(True)
        bot.cfg["video_browse_share_max_per_day"] = 0
        bot._vb_chat = Mock()
        self.assertIsNone(bot._vb_share_decide({"bvid": "BV1"}))
        bot._vb_chat.assert_not_called()
        bot.cfg["proactive_max_per_day"] = 0
        self.assertEqual(bot._vb_proactive_left("p:10"), 0)

    def test_manual_completion_and_budget_reply_bypass_auto_contact(self):
        self.contact.allowed = False
        bot = self.bot(False)
        st = self.state(source="manual", watched=2, titles=["测试视频"])
        bot._vb_run(st)
        bot._vb_say.assert_called_once()
        self.attempt.assert_not_called()
        bot._vb_say.reset_mock()
        self.assertTrue(bot._vb_token_notice("p:10", False, 0, 10, force=True))
        bot._vb_say.assert_called_once()

    def test_blocked_auto_budget_notice_skips_generation_and_is_not_retried(self):
        self.contact.allowed = False
        bot = self.bot(True)
        self.assertFalse(bot._vb_token_notice("p:10", False, 0, 10))
        self.assertFalse(bot._vb_token_notice("p:10", False, 0, 10))
        bot.pool.chat.assert_not_called()
        bot._vb_say.assert_not_called()
        self.attempt.assert_called_once()

    def test_pending_videos_never_burst_after_restart_or_contact_resumes(self):
        bot = self.bot(True)
        bot._vb_pending_set([{"bvid": "BV1"}, {"bvid": "BV2"}])
        bot._vb_share_send = Mock(return_value=True)
        self.assertEqual(bot._vb_retry_pending_shares(), 0)
        bot._vb_share_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
