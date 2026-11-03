import copy
import json
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'runtime', 'core'))
import private_contact as pc


class MemoryStore:
    def __init__(self, settings=None, rows=None):
        self.settings = settings if settings is not None else {}
        self.rows = rows or []

    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value):
        self.settings[key] = value

    def thread_rows(self, conv, limit):
        return copy.deepcopy(self.rows[-limit:])

    def is_blocked(self, uid):
        return False


class PrivateContactTests(unittest.TestCase):
    def bot(self, store=None):
        bot = SimpleNamespace(cfg={"owner_qq": 10, "reply_autonomy_enabled": False},
                              store=store or MemoryStore(), now=2000000000)
        bot._proactive_now = lambda: bot.now
        bot._is_proactive_quiet = lambda stamp: False
        return bot

    def sent(self, bot, kind="ordinary", key="first", material=""):
        with pc.attempt(bot, "p:10", kind=kind, event_key=key, material=material) as contact:
            self.assertTrue(contact.allowed, contact.reason)
            self.assertTrue(contact.sending(), contact.reason)
            self.assertTrue(contact.sent())

    def allowed(self, bot, kind="ordinary", key="next", material="", conv="p:10"):
        with pc.attempt(bot, conv, kind=kind, event_key=key, material=material) as contact:
            return contact.allowed

    def test_unanswered_streams_share_one_contact_budget(self):
        bot = self.bot()
        self.sent(bot, kind="video")
        bot.now += 3 * pc.HOUR
        for kind in ("ordinary", "care", "share", "video", "video_notice"):
            with self.subTest(kind=kind):
                self.assertFalse(self.allowed(bot, kind=kind))
        bot.now += 10 * pc.HOUR
        self.assertTrue(self.allowed(bot, kind="care"))
        self.assertFalse(self.allowed(bot, kind="video"))

    def test_unanswered_is_backoff_not_permanent_silence(self):
        bot = self.bot()
        self.sent(bot)
        bot.now += pc.DAY
        self.assertTrue(self.allowed(bot, kind="ordinary"))
        self.assertFalse(self.allowed(bot, kind="video"))
        bot.now += pc.DAY
        self.assertTrue(self.allowed(bot, kind="video"))

    def test_second_unanswered_video_waits_longer(self):
        bot = self.bot()
        self.sent(bot, kind="video", key="v1")
        bot.now += 2 * pc.DAY
        self.sent(bot, kind="video", key="v2")
        bot.now += 2 * pc.DAY
        self.assertFalse(self.allowed(bot, kind="video", key="v3"))
        bot.now += 5 * pc.DAY
        self.assertTrue(self.allowed(bot, kind="video", key="v3"))

    def test_clear_sleep_busy_and_stop_intents(self):
        bot = self.bot()
        for text, kind, duration in (("我先睡觉了，晚安", "sleep", 8 * pc.HOUR),
                                     ("午睡一会", "sleep", 90 * 60),
                                     ("我在开会，晚点聊", "busy", 2 * pc.HOUR),
                                     ("别再给我发消息了", "stop", 0)):
            with self.subTest(text=text):
                found, until = pc.boundary(text, bot.now)
                self.assertEqual(found, kind)
                self.assertEqual(until, bot.now + duration if duration else 0)

    def test_negations_quotes_hypotheticals_and_questions_are_not_rest(self):
        for text in ("我不睡了", "我还没睡", "我没说晚安", "我不是要休息",
                     "我不忙", "他说晚安了", '他说“我要睡觉了”',
                     "> 晚安\n这句话是引用", "假如我说晚安，你会怎么办", "你要睡觉了吗？",
                     "晚安是什么意思", "你说晚安是要睡觉吗", "不要给我答案，我想自己算",
                     "不用给我发链接，发正文就好", "我睡觉前会刷视频", "我睡眠不好",
                     "我没说别打扰我", "我没说不要找我", "别停止私信", "不必停止私信",
                     "我昨天睡了八小时", "我已经睡了8个小时", "明天我要上班"):
            with self.subTest(text=text):
                self.assertEqual(pc.boundary(text, 2000000000), ("", 0))

    def test_sleep_blocks_ordinary_video_and_care_until_direct_return(self):
        bot = self.bot()
        pc.observe(bot, "p:10", "我先睡了，晚安", message_id="night")
        bot.now += pc.HOUR
        for kind in ("ordinary", "video", "video_notice", "share", "care"):
            self.assertFalse(self.allowed(bot, kind=kind), kind)
        pc.observe(bot, "p:10", "我醒了", message_id="wake")
        self.assertFalse(self.allowed(bot))
        bot.now += 20 * 60
        self.assertTrue(self.allowed(bot))

    def test_busy_expires_naturally(self):
        bot = self.bot()
        pc.observe(bot, "p:10", "我去上课，晚点聊", message_id="busy")
        bot.now += pc.HOUR
        self.assertFalse(self.allowed(bot))
        bot.now += pc.HOUR
        self.assertTrue(self.allowed(bot))

    def test_explicit_stop_survives_unrelated_replies_and_emotional_exception(self):
        bot = self.bot()
        pc.observe(bot, "p:10", "不要再给我发消息", message_id="stop")
        bot.now += pc.DAY
        pc.observe(bot, "p:10", "帮我算一下这道题", message_id="question")
        bot.now += pc.HOUR
        self.assertFalse(self.allowed(bot))
        self.assertFalse(self.allowed(bot, kind="dream", material="被噩梦吓醒"))
        pc.observe(bot, "p:10", "可以主动找我", message_id="resume")
        bot.now += pc.HOUR
        self.assertTrue(self.allowed(bot))

    def test_group_activity_does_not_clear_unanswered_or_reopen_sleep(self):
        bot = self.bot()
        self.sent(bot, kind="video")
        pc.observe(bot, "p:10", "我去睡了", private=False, message_id="group-night")
        bot.now += pc.HOUR
        pc.observe(bot, "p:10", "群里这个表情好好笑", private=False, message_id="group-chat")
        state = json.loads(bot.store.get_setting(pc.PREFIX + "p:10"))
        self.assertEqual(state["boundary"], "sleep")
        self.assertEqual(len(state["pending"]), 1)
        self.assertFalse(self.allowed(bot, kind="video"))

    def test_private_return_clears_pending_but_keeps_rolling_gap(self):
        bot = self.bot()
        self.sent(bot, kind="video")
        bot.now += pc.HOUR
        pc.observe(bot, "p:10", "刚才去忙了", message_id="return")
        bot.now += 20 * 60
        self.assertFalse(self.allowed(bot, kind="video"))
        bot.now += pc.HOUR
        self.assertTrue(self.allowed(bot, kind="video"))

    def test_restart_retains_unanswered_sleep_and_event_dedupe(self):
        bot = self.bot()
        self.sent(bot, kind="video", key="v1")
        bot.now += pc.HOUR
        pc.observe(bot, "p:10", "晚安", private=False, message_id="group-night")
        restarted = self.bot(bot.store)
        restarted.now = bot.now + pc.HOUR
        self.assertFalse(self.allowed(restarted, kind="video", key="new"))
        restarted.now += 10 * pc.DAY
        self.assertFalse(self.allowed(restarted, kind="video", key="v1"))
        self.assertTrue(self.allowed(restarted, kind="video", key="new"))

    def test_upgrade_seeds_legacy_unanswered_and_recent_goodnight(self):
        now = 2000000000
        store = MemoryStore(settings={"proactive:await:p:10": str(now - 60)},
                            rows=[{"id": 1, "ts": now - 120, "direction": "in", "text": "晚安"}])
        bot = self.bot(store)
        self.assertFalse(self.allowed(bot, kind="ordinary"))
        bot.now += 10 * pc.HOUR
        self.assertFalse(self.allowed(bot, kind="video"))

    def test_unknown_transport_attempt_is_persistent_and_never_retried(self):
        bot = self.bot()
        with pc.attempt(bot, "p:10", kind="video", event_key="unknown") as contact:
            self.assertTrue(contact.sending())
            # A timeout can already have delivered the message; intentionally no sent().
        restarted = self.bot(bot.store)
        restarted.now += 10 * pc.DAY
        self.assertFalse(self.allowed(restarted, kind="video", key="unknown"))
        state = json.loads(bot.store.get_setting(pc.PREFIX + "p:10"))
        self.assertEqual(state["events"][0]["status"], "attempted")

    def test_same_event_cannot_retry_under_a_different_kind(self):
        bot = self.bot()
        self.sent(bot, kind="video", key="same-source")
        bot.now += 4 * pc.DAY
        for kind in ("ordinary", "video_notice", "share", "tattle", "dream"):
            self.assertFalse(self.allowed(bot, kind=kind, key="same-source", material="噩梦惊醒"), kind)
        self.assertTrue(self.allowed(bot, kind="tattle", key="different-source", material="确实受了委屈"))

    def test_upgrade_inherits_old_video_and_share_contacts_and_persists_seed(self):
        now = 2000000000
        store = MemoryStore(rows=[{"id": 1, "ts": now - pc.DAY, "direction": "in", "text": "今天还挺忙"}])
        store.last_share = Mock(return_value={"id": 8, "ts": now - 2 * pc.HOUR,
                                             "kind": "share", "status": "pending"})
        bot = self.bot(store)
        bot._vb_shares = Mock(return_value=[{"ts": now - pc.HOUR, "bvid": "BV-old"}])
        self.assertFalse(self.allowed(bot, kind="video", key="bvid:new"))
        saved = store.get_setting(pc.PREFIX + "p:10")
        self.assertTrue(saved)
        state = json.loads(saved)
        self.assertEqual([row["kind"] for row in state["pending"]], ["share", "video"])
        bot.now += 8 * pc.DAY
        self.assertFalse(self.allowed(bot, kind="share", key="bvid:BV-old"))
        self.assertTrue(self.allowed(bot, kind="video", key="bvid:new"))
        store.last_share.assert_called_once()
        bot._vb_shares.assert_called_once()

    def test_upgrade_preserves_old_stop_through_unrelated_questions(self):
        now = 2000000000
        store = MemoryStore(rows=[
            {"id": 1, "ts": now - 2 * pc.DAY, "direction": "in", "text": "别再主动找我"},
            {"id": 2, "ts": now - pc.DAY, "direction": "in", "text": "帮我算一下这题"}])
        bot = self.bot(store)
        self.assertFalse(self.allowed(bot, kind="video"))
        self.assertEqual(json.loads(store.get_setting(pc.PREFIX + "p:10"))["boundary"], "stop")
        pc.observe(bot, "p:10", "不必停止私信", message_id="resume")
        bot.now += pc.HOUR
        self.assertTrue(self.allowed(bot))

    def test_asr_adds_boundary_for_same_message_without_resetting_contact_again(self):
        bot = self.bot()
        pc.observe(bot, "p:10", "", message_id="record-1")
        state_before = json.loads(bot.store.get_setting(pc.PREFIX + "p:10"))
        pc.observe(bot, "p:10", "我先睡了，晚安", message_id="record-1", boundary_only=True)
        state = json.loads(bot.store.get_setting(pc.PREFIX + "p:10"))
        self.assertEqual(state["boundary"], "sleep")
        self.assertEqual(state["in_at"], state_before["in_at"])
        self.assertEqual(state["in_id"], "record-1")
        self.assertGreater(state["revision"], state_before["revision"])
        bot.now += pc.HOUR
        self.assertFalse(self.allowed(bot))

    def test_older_asr_result_cannot_overwrite_a_newer_private_return(self):
        bot = self.bot()
        first = bot.now
        pc.observe(bot, "p:10", "", message_id="record-1", ts=first)
        bot.now += 30
        pc.observe(bot, "p:10", "我刚才是开玩笑的，继续聊", message_id="newer")
        pc.observe(bot, "p:10", "我要睡觉了", ts=first, message_id="record-1", boundary_only=True)
        state = json.loads(bot.store.get_setting(pc.PREFIX + "p:10"))
        self.assertEqual(state["boundary"], "")
        self.assertEqual(state["in_id"], "newer")

    def test_tattle_night_exception_requires_independent_strict_model_boolean(self):
        bot = self.bot()
        evidence = {"direction": "in", "ts": bot.now - 60, "text": "你这只废物，滚远点", "juice": 5}
        bot.pool = SimpleNamespace(chat=Mock())
        for text, expected in (("{\"distressed\":true}", True), ("{\"distressed\":false}", False),
                               ("{\"distressed\":\"true\"}", False), ("{\"distressed\":1}", False),
                               ("not json", False), ("{}", False)):
            with self.subTest(text=text):
                bot.pool.chat.return_value = SimpleNamespace(ok=True, text=text, tool_calls=[])
                self.assertEqual(pc.tattle_exception(bot, evidence, bot.now), expected)
        bot.pool.chat.return_value = SimpleNamespace(ok=False, text="{\"distressed\":true}")
        self.assertFalse(pc.tattle_exception(bot, evidence, bot.now))
        bot.pool.chat.return_value = SimpleNamespace(ok=True, text="{\"distressed\":true}", tool_calls=[{}])
        self.assertFalse(pc.tattle_exception(bot, evidence, bot.now))
        bot.pool.chat.side_effect = TimeoutError("fake timeout")
        self.assertFalse(pc.tattle_exception(bot, evidence, bot.now))

    def test_neutral_tattle_and_stale_evidence_cannot_use_juice_to_bypass(self):
        bot = self.bot()
        bot.pool = SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=True, text='{"distressed":false}')))
        evidence = {"direction": "in", "ts": bot.now - 60, "text": "你今天吃什么？", "juice": 5}
        self.assertFalse(pc.tattle_exception(bot, evidence, bot.now))
        bot.pool.chat.assert_called_once()
        bot.pool.chat.reset_mock()
        for bad in (dict(evidence, direction="out"), dict(evidence, ts=bot.now - 6 * pc.HOUR),
                    dict(evidence, ts=bot.now + 1), dict(evidence, text="")):
            self.assertFalse(pc.tattle_exception(bot, bad, bot.now))
        bot.pool.chat.assert_not_called()

    def test_incoming_during_generation_cancels_the_prepared_message(self):
        bot = self.bot()
        with pc.attempt(bot, "p:10", event_key="prepared") as contact:
            self.assertTrue(contact.allowed)
            bot.now += 1
            worker = threading.Thread(target=pc.observe,
                                      args=(bot, "p:10", "晚安"), kwargs={"message_id": "new"})
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(contact.check())
            self.assertFalse(contact.sending())
        state = json.loads(bot.store.get_setting(pc.PREFIX + "p:10"))
        self.assertEqual(state["events"], [])

    def test_concurrent_streams_only_reserve_one_private_contact(self):
        bot = self.bot()
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(kind):
            with pc.attempt(bot, "p:10", kind=kind, event_key=kind) as contact:
                barrier.wait(timeout=2)
                reserved = contact.sending()
                outcomes.append(reserved)
                if reserved:
                    contact.sent()

        workers = [threading.Thread(target=worker, args=(kind,)) for kind in ("ordinary", "video")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(outcomes), [False, True])

    def test_emotional_nightmare_can_reach_owner_during_sleep_without_demanding_reply(self):
        bot = self.bot()
        pc.observe(bot, "p:10", "晚安，我睡觉了", message_id="night")
        bot.now += pc.HOUR
        self.sent(bot, kind="dream", key="nightmare-1", material="做了噩梦，刚刚被吓醒")
        bot.now += 11 * pc.HOUR
        self.assertFalse(self.allowed(bot, kind="tattle", key="upset-1", material="被欺负了很委屈"))
        bot.now += pc.HOUR
        self.assertTrue(self.allowed(bot, kind="tattle", key="upset-1", material="被欺负了很委屈"))

    def test_two_emotional_contacts_without_reply_exhaust_72_hour_exception(self):
        bot = self.bot()
        self.sent(bot, kind="tattle", key="event-1", material="被人凶了有点委屈")
        bot.now += 12 * pc.HOUR
        self.sent(bot, kind="dream", key="event-2", material="噩梦惊醒")
        bot.now += 12 * pc.HOUR
        self.assertFalse(self.allowed(bot, kind="dream", key="event-3", material="又做噩梦吓醒"))
        bot.now += 49 * pc.HOUR
        self.assertTrue(self.allowed(bot, kind="dream", key="event-3", material="又做噩梦吓醒"))

    def test_ordinary_dream_and_nonowner_emotions_do_not_get_exception(self):
        bot = self.bot()
        self.assertFalse(self.allowed(bot, kind="dream", material="梦到吃蛋糕和看海"))
        pc.observe(bot, "p:20", "晚安", message_id="friend-night")
        bot.now += pc.HOUR
        self.assertFalse(self.allowed(bot, kind="dream", conv="p:20", material="噩梦惊醒"))

    def test_hook_care_requires_fresh_first_person_concern(self):
        now = 2000000000
        for text in ("我今天发烧了", "我明天考试有点焦虑"):
            self.assertEqual(pc.classify_hook({"type": "thread", "src_ts": now - 60, "material": text}, now), "care")
        for hook in ({"type": "thread", "src_ts": now - 3 * pc.DAY, "material": "我发烧了"},
                     {"type": "memory", "src_ts": now, "material": "我发烧了"},
                     {"type": "thread", "src_ts": now, "material": "我朋友明天考试"},
                     {"type": "thread", "src_ts": now, "material": "我没生病"},
                     {"type": "thread", "src_ts": now, "material": "我今天吃面条了", "strength": "strong"}):
            self.assertEqual(pc.classify_hook(hook, now), "ordinary")

    def test_contact_state_never_persists_chat_material(self):
        bot = self.bot()
        secret_text = "我生病了，这是只有我们知道的事情"
        pc.observe(bot, "p:10", secret_text, message_id="in")
        bot.now += pc.HOUR
        self.sent(bot, kind="care", material=secret_text)
        self.assertNotIn(secret_text, bot.store.get_setting(pc.PREFIX + "p:10"))


if __name__ == "__main__":
    unittest.main()
