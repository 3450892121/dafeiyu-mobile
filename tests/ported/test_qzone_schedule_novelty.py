import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.environ.get("BOT_TEST_PATH", os.path.join(os.path.dirname(__file__), '..', '..', 'runtime', 'core')))
import flow_qzone as fq
from store import Store


class QzoneScheduleTests(unittest.TestCase):
    def bot(self, **cfg):
        b = fq.QzoneMixin()
        b.cfg = {"qzone_enabled": True, "qzone_mode": "live", "qzone_daily_max": 6,
                 "qzone_active_hours": "5-2", **cfg}
        settings = {}
        b.store = SimpleNamespace(get_setting=lambda k: settings.get(k),
                                 set_setting=lambda k, v: settings.__setitem__(k, v))
        b._qz_log = Mock()
        b._qz_today = Mock(return_value="2030-01-02")
        b._qz_daily_count = Mock(return_value=0)
        b._qz_last_post_ts = Mock(return_value=0)
        return b, settings

    def test_random_limits_and_restart_persistence(self):
        for cap in (1, 3, 6, 20):
            for result in (1, min(cap, 6)):
                b, settings = self.bot(qzone_daily_max=cap)
                with patch.object(fq.random, "randint", return_value=result) as rnd:
                    self.assertEqual(b._qz_target_today(), result)
                    rnd.assert_called_once_with(1, min(cap, 6))
                    restored, _ = self.bot(qzone_daily_max=cap)
                    restored.store = b.store
                    self.assertEqual(restored._qz_target_today(), result)
                    self.assertEqual(rnd.call_count, 1)
                self.assertEqual(len(settings), 1)

    def test_zero_disables_and_cached_value_obeys_new_cap(self):
        b, settings = self.bot(qzone_daily_max=0)
        settings["qzone:target:2030-01-02"] = "5"
        self.assertEqual(b._qz_target_today(), 0)
        self.assertIn("已够", b._qz_can_post()[1])
        b.cfg["qzone_daily_max"] = 2
        self.assertEqual(b._qz_target_today(), 2)
        b.cfg["qzone_daily_max"] = 20
        settings["qzone:target:2030-01-02"] = "99"
        self.assertEqual(b._qz_target_today(), 6)

    def test_new_day_draws_again(self):
        b, _ = self.bot()
        with patch.object(fq.random, "randint", side_effect=[2, 6]):
            self.assertEqual(b._qz_target_today(), 2)
            b._qz_today.return_value = "2030-01-03"
            self.assertEqual(b._qz_target_today(), 6)

    def test_opportunities_cover_active_day_including_overnight(self):
        for hours in ("5-2", "9-23", "0-24", "bad"):
            for target in range(1, 7):
                with self.subTest(hours=hours, target=target):
                    b, _ = self.bot(qzone_active_hours=hours)
                    with patch.object(fq.random, "randrange", side_effect=lambda lo, hi: lo):
                        at = b._qz_release_times(target)
                    self.assertEqual(len(at), target)
                    self.assertEqual(at, sorted(set(at)))
                    if hours == "5-2":
                        self.assertTrue(all(n < 120 or n >= 300 for n in at))
                        if target >= 3:
                            self.assertGreaterEqual(at[-1], 16 * 60)
                    if hours == "9-23":
                        self.assertTrue(all(9 * 60 <= n < 23 * 60 for n in at))
                    with patch.object(fq.random, "randrange", side_effect=AssertionError("redraw")):
                        self.assertEqual(b._qz_release_times(target), at)

    def test_empty_hours_and_corrupt_plan(self):
        b, settings = self.bot(qzone_active_hours="5-5")
        self.assertEqual(b._qz_release_times(6), [])
        b.cfg["qzone_active_hours"] = "5-2"
        settings["qzone:plan:2030-01-02"] = "{bad"
        at = b._qz_release_times(3)
        self.assertEqual(len(at), 3)
        settings["qzone:plan:2030-01-02"] = json.dumps({"target": 3, "hours": "5-2", "at": [1, 1, 1]})
        self.assertEqual(len(set(b._qz_release_times(3))), 3)

    def test_no_frontloading_and_respects_manual_count_interval_cooldown(self):
        b, settings = self.bot()
        settings["qzone:target:2030-01-02"] = "3"
        settings["qzone:plan:2030-01-02"] = json.dumps({"target": 3, "hours": "5-2", "at": [0, 12*60, 19*60]})
        def clock(hour):
            return patch.object(fq.time, "localtime", return_value=time.struct_time((2030, 1, 2, hour, 0, 0, 2, 2, -1)))
        with clock(7):
            self.assertTrue(b._qz_can_post()[0])
            b._qz_daily_count.return_value = 1
            self.assertIn("下个", b._qz_can_post()[1])
        with clock(13):
            self.assertTrue(b._qz_can_post()[0])
            b._qz_last_post_ts.return_value = time.time() - 60
            self.assertIn("太近", b._qz_can_post()[1])
            b._qz_last_post_ts.return_value = 0
            settings["qzone:cooldown_until"] = str(time.time()+3600)
            self.assertIn("冷却", b._qz_can_post()[1])
        b._qz_daily_count.assert_called_with()
        b._qz_daily_count.return_value = 3
        with clock(20):
            self.assertIn("已够", b._qz_can_post()[1])

    def test_draft_history_excluded_with_real_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "bot.db"))
            try:
                for i, status in enumerate(("posted", "queued", "deleted", "blocked", "drafted", "failed")):
                    store.add_qzone_post("auto", status+"正文", str(i), status=status)
                self.assertEqual(set(store.recent_qzone_texts(24)), {s+"正文" for s in ("posted", "queued", "deleted")})
            finally:
                store.conn.close()

    def pipeline(self):
        b, _ = self.bot()
        b._qz_can_post = Mock(return_value=(True, "live"))
        b._qz_materials = Mock(return_value={"diary": "日记", "recent": ["以前的一条说说"]})
        b._qz_decide = Mock(return_value={"worth": True, "picks": [{"event": "旧题", "mood": "开心"}, {"event": "新题", "mood": "开心"}]})
        b._qz_compose = Mock(return_value=("新写的内容", {}))
        b._qz_model_check = Mock(return_value=(True, {}))
        b._qz_choose_image = Mock(return_value=None)
        b._qz_publish = Mock(return_value=True)
        b._qz_record_blocked = Mock()
        b._qz_schedule_next = Mock()
        return b

    def test_many_manual_or_failed_records_do_not_hide_autonomous_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "bot.db"))
            try:
                for i in range(6):
                    store.add_qzone_post("auto", "已发说说", "auto"+str(i), status="deleted" if i == 0 else "posted")
                for i in range(60):
                    store.add_qzone_post("manual", "手动说说", "manual"+str(i), status="posted")
                    store.add_qzone_post("auto", "失败说说", "failed"+str(i), status="failed")
                b, _ = self.bot()
                b.store = store
                self.assertEqual(fq.QzoneMixin._qz_daily_count(b), 6)
                self.assertEqual(fq.QzoneMixin._qz_daily_count(b, True), 66)
            finally:
                store.conn.close()

    def test_count_failure_prevents_autonomous_post(self):
        b, _ = self.bot()
        self.assertEqual(fq.QzoneMixin._qz_daily_count(b), 6)

    def test_changed_quota_or_switch_during_generation_prevents_publish(self):
        b = self.pipeline()
        b._ask = Mock(return_value={"duplicate": False})
        b._qz_can_post.side_effect = [(True, "live"), (False, "总开关关着")]
        b._qz_tick()
        b._qz_publish.assert_not_called()

    def test_manual_request_keeps_existing_quota_and_novelty_bypass(self):
        b = self.pipeline()
        b.cfg["qzone_daily_max"] = 0
        b.cfg["qzone_enabled"] = False
        b._qz_manual_context = Mock(return_value="主人点题的内容")
        b._qz_novelty_check = Mock(side_effect=AssertionError("manual novelty gate"))
        result = b._qz_manual_post("主动要求发的题目")
        self.assertTrue(result["ok"])
        b._qz_can_post.assert_not_called()
        b._qz_publish.assert_called_once()
        self.assertEqual(b._qz_publish.call_args.args[2], "manual")
        self.assertTrue(b._qz_compose.call_args.kwargs["force"])

    def test_duplicate_topic_skipped_before_writing(self):
        b = self.pipeline()
        b._ask = Mock(side_effect=[{"duplicate": True, "why": "同一场景"}, {"duplicate": False}, {"duplicate": False}])
        b._qz_tick()
        b._qz_compose.assert_called_once()
        self.assertEqual(b._qz_compose.call_args.args[0]["event"], "新题")
        b._qz_publish.assert_called_once()

    def test_final_rewrite_of_old_topic_cannot_publish(self):
        b = self.pipeline()
        b._ask = Mock(side_effect=[{"duplicate": False}, {"duplicate": True}, {"duplicate": False}, {"duplicate": True}])
        b._qz_tick()
        b._qz_publish.assert_not_called()
        b._qz_schedule_next.assert_called_once()

    def test_novelty_failure_is_closed_without_history_does_not_call_model(self):
        b, _ = self.bot()
        b._ask = Mock()
        self.assertTrue(b._qz_novelty_check("文字", [])[0])
        b._ask.assert_not_called()
        for response in (None, {}, {"duplicate": "false"}, {"duplicate": 0}):
            b._ask.return_value = response
            self.assertFalse(b._qz_novelty_check("文字", ["历史"])[0])

    def test_novelty_unavailable_sends_nothing(self):
        b = self.pipeline()
        b._ask = Mock(return_value=None)
        b._qz_tick()
        b._qz_compose.assert_not_called()
        b._qz_publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
