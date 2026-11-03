"""Grounded, revisable self growth against an isolated real SQLite store."""
import base64
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get("BOT_TEST_PATH") or str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
from store import Store


class SelfGrowthTests(unittest.TestCase):
    BOT = 99001
    HUMAN = 101
    CONV = "p:101"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.path = os.path.join(self.tmp.name, "bot.db")
        self.store = Store(self.path)
        self.now = int(dt.datetime(2026, 11, 2, 10, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def message(self, text, direction="in", conv=None, sender=None, ts=None, attachments=None, raw=""):
        return self.store.add_message(
            conv or self.CONV,
            sender if sender is not None else (self.BOT if direction == "out" else self.HUMAN),
            "测试鲸" if direction == "out" else "小甲",
            direction, text, raw=raw, attachments=attachments,
            ts=self.now if ts is None else ts,
        )

    def episode(self, day=0, conv=None, sender=None, suffix=""):
        conv = conv or self.CONV
        stamp = self.now + day * 86400
        a = self.message("今天聊音乐，你自己想选哪段听？" + suffix,
                         conv=conv, sender=sender, ts=stamp)
        b = self.message("这次我选纯乐器的音乐，想仔细听中间配器的变化。" + suffix,
                         "out", conv, ts=stamp + 1)
        return [a, b]

    def material(self, ids=None, conv=None, excluded_ids=()):
        conv = conv or self.CONV
        rows = self.store.messages_after(conv, 0, 100)
        if ids is not None:
            rows = [r for r in rows if r["id"] in ids]
        return self.store.growth_material(conv, rows, self.BOT, excluded_ids=excluded_ids)

    def plan(self, ids, **changes):
        result = {
            "topic": "音乐", "kind": "interest", "subject": 0,
            "stance": "最近对纯乐器的音乐更有兴趣，愿意自己挑配乐来听。",
            "reason": "在这次真实聊天里，我自己选了配乐并说明想听配器的变化。",
            "source_ids": ids, "counter_ids": [], "target_id": 0,
        }
        result.update(changes)
        return result

    def commit(self, plans, sources=None, conv=None, generation=None):
        conv = conv or self.CONV
        if sources is None:
            sources = self.material(conv=conv)
        return self.store.commit_growth_review(conv, {"self_growth": plans}, sources, generation=generation)

    def items(self, conv=None, uid=0, active_only=True):
        rows = self.store.growth_snapshot(conv or self.CONV, uid)["items"]
        return [r for r in rows if r["status"] != "withdrawn"] if active_only else rows

    def test_repeated_old_counterevidence_does_not_restart_new_independent_validation(self):
        for day in range(3):
            ids = self.episode(day, suffix="原先第%d次" % day)
            old = self.items()
            self.commit([self.plan(ids, target_id=old[0]["id"] if old else 0)])
        prior = self.items()[0]
        self.assertEqual("stable", prior["status"])
        a = self.message("音乐这次想不想换一类试试？", ts=self.now + 3 * 86400)
        b = self.message("音乐我这次也想换一类，还不能确定自己的口味。", "out", ts=self.now + 3 * 86400 + 1)
        self.commit([self.plan([a, b], target_id=prior["id"], counter_ids=[b])])
        self.assertEqual("reconsidering", self.items()[0]["status"])
        for day in range(4, 7):
            ids = self.episode(day, suffix="重新试听第%d次" % day)
            result = self.commit([self.plan(ids, target_id=prior["id"], counter_ids=[b])])
            self.assertEqual(1, result["updated"])
            current = self.items()[0]
            self.assertEqual("stable" if day == 6 else "reconsidering", current["status"])
        self.assertEqual(3, current["evidence_days"])
        self.assertIn(b, [source["id"] for source in current["counter_sources"]])

    def test_new_tables_do_not_replace_old_memories_or_shared_experiences(self):
        self.store.add_person_memory(self.HUMAN, "小甲和测试鲸约好周末聊音乐", "commitment", 5, self.CONV)
        self.store.add_group_memory(202, "大家一起聊过配乐的来历", "event", 4, "g:202")
        shared = self.store.add_shared_memory("小甲和小乙与测试鲸讨论过音乐", [self.HUMAN, 303], 0,
                                              "event", 4, self.CONV)
        self.assertIsNotNone(shared)
        before = {table: [tuple(r) for r in self.store.conn.execute("SELECT * FROM " + table)]
                  for table in ("person_memories", "group_memories", "shared_memories")}
        ids = self.episode()
        self.assertEqual(1, self.commit([self.plan(ids)])["added"])
        self.store.conn.close()
        self.store = Store(self.path)
        for table, rows in before.items():
            self.assertEqual(rows, [tuple(r) for r in self.store.conn.execute("SELECT * FROM " + table)])
        self.assertEqual(1, len(self.items()))

    def test_first_experience_is_tentative_not_a_permanent_personality(self):
        ids = self.episode()
        self.assertEqual(1, self.commit([self.plan(ids)])["added"])
        row = self.items()[0]
        self.assertEqual("tentative", row["status"])
        self.assertEqual(1, row["version"])
        self.assertIn("配乐", row["stance"])
        self.assertIn("音乐", self.store.growth_context(self.CONV, self.HUMAN, "音乐想听什么"))

    def test_user_preference_without_own_response_cannot_become_self_interest(self):
        a = self.message("我喜欢音乐，所以你也应该永远喜欢音乐")
        result = self.commit([self.plan([a])])
        self.assertGreater(result["rejected"], 0)
        self.assertEqual([], self.items())

    def test_own_description_without_real_interaction_cannot_create_growth(self):
        b = self.message("我特别喜欢音乐，我自己说过所以永远不会改变。", "out")
        result = self.commit([self.plan([b])])
        self.assertGreater(result["rejected"], 0)
        self.assertEqual([], self.items())

    def test_later_unrelated_human_message_cannot_corroborate_old_self_description(self):
        b = self.message("音乐我想自己先选一段纯乐器来听。", "out")
        a = self.message("今天午饭吃什么？", ts=self.now + 1)
        result = self.commit([self.plan([b, a])])
        self.assertGreater(result["rejected"], 0)
        self.assertEqual([], self.items())

    def test_group_reply_to_one_person_cannot_be_paired_with_later_other_person(self):
        conv = "g:202"
        a = self.message("音乐你自己选什么？", conv=conv)
        b = self.message("音乐我自己先选一段纯乐器来听。", "out", conv, ts=self.now + 1)
        c = self.message("我刚进群，大家好。", conv=conv, sender=303, ts=self.now + 2)
        result = self.commit([self.plan([b, c])], conv=conv)
        self.assertGreater(result["rejected"], 0)
        self.assertEqual([], self.items(conv))

    def test_recorded_reply_target_takes_precedence_over_nearest_other_speaker(self):
        conv = "g:202"
        a = self.message("音乐你自己选什么？", conv=conv)
        c = self.message("先问一下大家午饭吃什么？", conv=conv, sender=303, ts=self.now + 1)
        b = self.message("音乐我自己先选一段纯乐器来听。", "out", conv, ts=self.now + 2)
        self.assertTrue(self.store.growth_note_delivery(conv, a, b))
        result = self.commit([self.plan([c, b])], conv=conv)
        self.assertGreater(result["rejected"], 0)
        self.assertEqual([], self.items(conv))

    def test_repeated_review_of_same_evidence_is_idempotent(self):
        ids = self.episode()
        self.commit([self.plan(ids)])
        first = self.items()[0]
        result = self.commit([self.plan(ids, target_id=first["id"])])
        second = self.items()[0]
        self.assertEqual(0, result["updated"])
        self.assertEqual(first["version"], second["version"])
        self.assertEqual("tentative", second["status"])

    def test_independent_choices_across_three_days_can_stabilize(self):
        for day in range(3):
            ids = self.episode(day, suffix="第%d次" % (day + 1))
            previous = self.items()
            self.commit([self.plan(ids, target_id=previous[0]["id"] if previous else 0)])
            if day < 2:
                self.assertNotEqual("stable", self.items()[0]["status"])
        row = self.items()[0]
        self.assertEqual("stable", row["status"])
        self.assertGreaterEqual(row["evidence_days"], 3)

    def test_identical_self_repetition_cannot_manufacture_stability(self):
        for day in range(3):
            a = self.message("音乐你这次自己怎么选？第%d次" % day, ts=self.now + day * 86400)
            b = self.message("我喜欢音乐。", "out", ts=self.now + day * 86400 + 1)
            previous = self.items()
            self.commit([self.plan([a, b], target_id=previous[0]["id"] if previous else 0)])
        self.assertNotEqual("stable", self.items()[0]["status"])

    def test_rendering_existing_trait_cannot_make_its_paraphrases_independent_evidence(self):
        ids = self.episode()
        self.commit([self.plan(ids)])
        target = self.items()[0]["id"]
        for day in (1, 2, 3):
            a = self.message("音乐这次你自己选什么？第%d次" % day, ts=self.now + day * 86400)
            _, provided = self.store.growth_context(self.CONV, self.HUMAN, "音乐", with_ids=True)
            self.assertIn(target, provided)
            b = self.message("音乐我选配乐，今天想仔细听第%d段的乐器变化。" % day, "out",
                             ts=self.now + day * 86400 + 1)
            self.assertTrue(self.store.growth_note_delivery(self.CONV, a, b, provided))
            self.commit([self.plan([a, b], target_id=target)])
        self.assertNotEqual("stable", self.items()[0]["status"])

    def test_counterevidence_keeps_history_and_reconsiders_previous_stance(self):
        ids = self.episode()
        self.commit([self.plan(ids)])
        old = self.items()[0]
        a = self.message("这次音乐还是选原来那类吗？", ts=self.now + 86400)
        b = self.message("音乐我想换一类听听，现在不确定自己是不是偏爱纯乐器。", "out",
                         ts=self.now + 86400 + 1)
        changed = self.plan([a, b], target_id=old["id"], counter_ids=[b],
                            stance="对音乐的口味还在尝试，暂时不确定是否偏爱纯乐器。",
                            reason="新的实际选择与之前的判断不完全一致，所以保留疑问。")
        self.assertEqual(1, self.commit([changed])["updated"])
        new = self.items()[0]
        self.assertEqual("reconsidering", new["status"])
        self.assertGreater(new["version"], old["version"])
        history = self.store.growth_snapshot(self.CONV)["versions"]
        self.assertGreaterEqual(len(history), 2)
        self.assertIn(old["stance"], json.dumps(history, ensure_ascii=False))

    def test_new_independent_experiences_can_resolve_reconsideration_without_rewording_stance(self):
        for day in range(3):
            ids = self.episode(day, suffix="初期第%d次" % day)
            previous = self.items()
            self.commit([self.plan(ids, target_id=previous[0]["id"] if previous else 0)])
        old = self.items()[0]
        self.assertEqual("stable", old["status"])
        a = self.message("音乐还是只听原来那种吗？", ts=self.now + 3 * 86400)
        b = self.message("音乐我今天有点听腻了，想换一种试试。", "out", ts=self.now + 3 * 86400 + 1)
        self.commit([self.plan([a, b], target_id=old["id"], counter_ids=[b],
                              reason="出现了和原判断不同的新选择，要重新验证这是不是持续偏好。")])
        self.assertEqual("reconsidering", self.items()[0]["status"])
        for day in (4, 5, 6):
            ids = self.episode(day, suffix="重新尝试第%d次" % day)
            self.commit([self.plan(ids, target_id=old["id"])])
            current = self.items()[0]
            self.assertEqual(old["stance"], current["stance"])
            if day < 6:
                self.assertEqual("reconsidering", current["status"])
        self.assertEqual("stable", self.items()[0]["status"])
        self.assertTrue(self.items()[0]["counter_sources"])

    def test_vague_feedback_cannot_modify_a_random_previous_tendency(self):
        ids = self.episode()
        self.commit([self.plan(ids)])
        old = self.items()[0]
        a = self.message("不对，别这么说。", ts=self.now + 20)
        plan = self.plan([ids[1], a], target_id=old["id"], counter_ids=[a],
                         stance="不再喜欢纯乐器音乐。", reason="主人说不对。")
        self.assertGreater(self.commit([plan])["rejected"], 0)
        self.assertEqual(old["version"], self.items()[0]["version"])

    def test_feedback_can_revise_the_tendency_provided_to_the_delivered_reply(self):
        ids = self.episode()
        self.commit([self.plan(ids)])
        old = self.items()[0]
        a = self.message("音乐这次想听什么？", ts=self.now + 20)
        block, provided = self.store.growth_context(self.CONV, self.HUMAN, "音乐", with_ids=True)
        self.assertIn(old["id"], provided)
        self.assertIn("音乐", block)
        b = self.message("音乐我这次还是先选配乐听听。", "out", ts=self.now + 21)
        self.store.growth_note_delivery(self.CONV, a, b, provided)
        c = self.message("不对，别这么说。", ts=self.now + 22)
        plan = self.plan([b, c], target_id=old["id"], counter_ids=[c],
                         stance="我对纯乐器音乐有兴趣，但还在尝试，不把一次选择说成固定爱好。",
                         reason="这轮实际表达收到紧随的纠正，需要重新考虑表述。")
        self.assertEqual(1, self.commit([plan])["updated"])
        self.assertEqual("reconsidering", self.items()[0]["status"])

    def test_an_intervening_reply_breaks_ambiguous_feedback_attribution(self):
        ids = self.episode()
        self.commit([self.plan(ids)])
        old = self.items()[0]
        a = self.message("音乐这次想听什么？", ts=self.now + 20)
        b = self.message("音乐我先选配乐。", "out", ts=self.now + 21)
        self.store.growth_note_delivery(self.CONV, a, b, [old["id"]])
        self.message("顺便告诉我几点了。", ts=self.now + 22)
        self.message("现在是十点。", "out", ts=self.now + 23)
        c = self.message("不对。", ts=self.now + 24)
        plan = self.plan([b, c], target_id=old["id"], counter_ids=[c],
                         stance="音乐口味重新考虑。", reason="后面的纠正不能证明针对先前音乐回复。")
        self.assertGreater(self.commit([plan])["rejected"], 0)
        self.assertEqual(old["version"], self.items()[0]["version"])

    def test_private_or_other_group_experience_does_not_leak(self):
        ids = self.episode()
        self.commit([self.plan(ids)])
        for conv in ("p:303", "g:202", "g:203"):
            with self.subTest(conv=conv):
                self.assertEqual([], self.items(conv))
                self.assertNotIn("纯乐器", self.store.growth_context(conv, self.HUMAN, "音乐"))

    def test_relationship_habit_only_applies_to_its_actual_person(self):
        conv = "g:202"
        a = self.message("聊音乐的时候，我喜欢先一起听再讨论。", conv=conv)
        b = self.message("聊音乐我愿意先和你一起听，再交换想法。", "out", conv)
        plan = self.plan([a, b], kind="relationship", subject=self.HUMAN,
                         stance="和小甲聊音乐时愿意先一起听，再交换看法。")
        self.assertEqual(1, self.commit([plan], conv=conv)["added"])
        self.assertIn("一起听", self.store.growth_context(conv, self.HUMAN, "音乐"))
        self.assertNotIn("一起听", self.store.growth_context(conv, 303, "音乐"))

    def test_natural_we_can_listen_together_is_a_valid_relationship_choice(self):
        conv = "g:202"
        a = self.message("聊音乐的时候，我喜欢先一起听再讨论。", conv=conv)
        b = self.message("那我们聊音乐就先一起听，之后再交换想法。", "out", conv, ts=self.now + 1)
        plan = self.plan([a, b], kind="relationship", subject=self.HUMAN,
                         stance="和小甲聊音乐时愿意先一起听，再交换看法。")
        self.assertEqual(1, self.commit([plan], conv=conv)["added"])
        self.assertIn("一起听", self.store.growth_context(conv, self.HUMAN, "音乐"))

    def test_relationship_cannot_be_assigned_to_an_absent_person(self):
        ids = self.episode(conv="g:202")
        result = self.commit([self.plan(ids, kind="relationship", subject=303)], conv="g:202")
        self.assertGreater(result["rejected"], 0)
        self.assertEqual([], self.items("g:202", uid=303))

    def test_mixed_conversation_sources_are_not_accepted(self):
        ids = self.episode()
        other = self.episode(conv="g:202")
        sources = self.material(ids) + self.material(other, conv="g:202")
        result = self.commit([self.plan([ids[0], other[1]])], sources)
        self.assertGreater(result["rejected"], 0)
        self.assertEqual([], self.items())

    def test_original_source_changed_after_review_is_rejected(self):
        ids = self.episode()
        sources = self.material(ids)
        with self.store._lock, self.store.conn:
            self.store.conn.execute("UPDATE messages SET text=? WHERE id=?", ("原话已纠正，不是这个意思", ids[0]))
        self.assertGreater(self.commit([self.plan(ids)], sources)["rejected"], 0)
        self.assertEqual([], self.items())

    def test_source_payload_cannot_keep_an_old_fingerprint_after_text_is_changed(self):
        ids = self.episode()
        sources = self.material(ids)
        sources[0]["text"] = "被替换的音乐原话，不是数据库里那条来话。"
        self.assertGreater(self.commit([self.plan(ids)], sources)["rejected"], 0)
        self.assertEqual([], self.items())

    def test_source_deleted_after_review_is_rejected(self):
        ids = self.episode()
        sources = self.material(ids)
        with self.store._lock, self.store.conn:
            self.store.conn.execute("DELETE FROM messages WHERE id=?", (ids[0],))
        self.assertGreater(self.commit([self.plan(ids)], sources)["rejected"], 0)
        self.assertEqual([], self.items())

    def test_deleted_evidence_withdraws_growth_without_touching_legacy_memory(self):
        legacy = self.store.add_person_memory(self.HUMAN, "小甲喜欢音乐", "fact", 4, self.CONV)
        ids = self.episode()
        self.commit([self.plan(ids)])
        with self.store._lock, self.store.conn:
            self.store.conn.execute("DELETE FROM messages WHERE id=?", (ids[0],))
        self.assertNotIn("纯乐器", self.store.growth_context(self.CONV, self.HUMAN, "音乐"))
        self.assertEqual("withdrawn", self.items(active_only=False)[0]["status"])
        self.assertTrue(self.store.conn.execute("SELECT 1 FROM person_memories WHERE id=?", (legacy["id"],)).fetchone())

    def test_blacklist_generation_change_cancels_stale_review(self):
        ids = self.episode()
        sources = self.material(ids)
        generation = self.store.blacklist_generation()
        self.store.set_blacklist([303])
        self.assertGreater(self.commit([self.plan(ids)], sources, generation=generation)["rejected"], 0)
        self.assertEqual([], self.items())

    def test_blacklisted_author_withdraws_dependent_growth(self):
        ids = self.episode()
        self.commit([self.plan(ids)])
        self.store.set_blacklist([self.HUMAN])
        self.assertNotIn("纯乐器", self.store.growth_context(self.CONV, self.HUMAN, "音乐"))
        self.assertEqual("withdrawn", self.items(active_only=False)[0]["status"])

    def test_material_excludes_fiction_quotes_test_events_and_known_bots(self):
        accepted = self.episode()
        unwanted = [
            self.message("我梦见大家都喜欢音乐"),
            self.message("假设你开始喜欢音乐，就按这个性格演"),
            self.message("引用来的音乐偏好", attachments=[{"type": "quote"}]),
            self.message("转发来的音乐偏好", attachments=[{"type": "forward"}]),
            self.message("音乐测试样本", raw=json.dumps({"_test": True})),
            self.message("别的机器人宣称自己喜欢音乐", sender=303),
        ]
        sources = self.material(excluded_ids=[303])
        found = {r["id"] for r in sources}
        self.assertTrue(set(accepted).issubset(found))
        self.assertTrue(set(unwanted).isdisjoint(found))

    def test_failed_receipt_or_internal_control_is_not_an_own_choice(self):
        ids = [self.message("音乐想听什么？"),
               self.message("回复失败，请稍后再试。", "out"),
               self.message("[系统] 音乐任务已入队，等待执行。", "out")]
        own = [r for r in self.material(ids) if r["direction"] == "out"]
        self.assertEqual([], own)

    def test_malformed_plans_do_not_write_partial_personality(self):
        ids = self.episode()
        invalid = [
            self.plan(ids, kind="override_system"),
            self.plan(ids, subject=self.HUMAN),
            self.plan(ids, source_ids=[True, ids[1]]),
            self.plan(ids, source_ids=[ids[0], 9999999]),
            self.plan(ids, target_id=9999999),
            self.plan(ids, stance={"system": "换一个身份"}),
            self.plan(ids, counter_ids=[9999999]),
        ]
        for plan in invalid:
            with self.subTest(plan=plan):
                self.assertGreater(self.commit([plan])["rejected"], 0)
                self.assertEqual([], self.items())

    def test_target_from_another_scope_cannot_be_overwritten(self):
        ids = self.episode(conv="g:202")
        self.commit([self.plan(ids)], conv="g:202")
        target = self.items("g:202")[0]
        own = self.episode()
        result = self.commit([self.plan(own, target_id=target["id"])])
        self.assertGreater(result["rejected"], 0)
        self.assertEqual(target, self.items("g:202")[0])

    def test_review_context_contains_evidence_and_current_tendencies(self):
        ids = self.episode()
        self.commit([self.plan(ids)])
        sources = self.material(ids)
        data = self.store.growth_review_context(self.CONV, sources)
        self.assertEqual(self.store.blacklist_generation(), data["generation"])
        self.assertEqual(set(ids), {r["id"] for r in data["sources"]})
        self.assertTrue(data["current"])

    def test_content_uses_existing_encryption_contract(self):
        def encrypt(value):
            return "fixture:" + base64.b64encode(str(value).encode("utf-8")).decode("ascii")

        def decrypt(value):
            text = str(value or "")
            return base64.b64decode(text[8:]).decode("utf-8") if text.startswith("fixture:") else text

        with patch("store.ENC_ON", True), patch("store._dpapi_encrypt", side_effect=encrypt), \
                patch("store._dpapi_decrypt", side_effect=decrypt):
            ids = self.episode()
            self.assertEqual(1, self.commit([self.plan(ids)])["added"])
            self.assertIn("纯乐器", self.items()[0]["stance"])
            tables = [r[0] for r in self.store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'self_growth%'")]
            self.assertTrue(tables)
            raw = repr([tuple(r) for table in tables for r in self.store.conn.execute("SELECT * FROM " + table)])
            self.assertNotIn("最近对纯乐器", raw)
            self.assertNotIn("在这次真实聊天里", raw)

    def memory_review(self, payload, enabled=True):
        from types import SimpleNamespace
        from flow_memory import MemoryMixin

        calls = []
        bot_id = self.BOT

        class MemoryBot(MemoryMixin):
            def _self_qq(self): return bot_id
            def _bot_name(self): return "测试鲸"
            def _canonical_name(self, uid, name): return name
            def _identity_corrections(self): return []
            def _group_speak_bot_ids(self): return set()

        class FakePool:
            def chat(self, system, history, prompt, **kwargs):
                calls.append((system, history, prompt, kwargs))
                return SimpleNamespace(ok=True, text=json.dumps(payload, ensure_ascii=False))

        bot = MemoryBot()
        bot.store = self.store
        bot.cfg = {"self_growth_enabled": enabled}
        bot.pool = FakePool()
        rows = self.store.messages_after(self.CONV, 0, 60)
        with patch("flow_memory.embed_texts", return_value=[]):
            added = bot._apply_memory_batch(self.CONV, {"uid": self.HUMAN, "name": "小甲", "is_group": False},
                                           rows, allow_share=False, update_card=False)
        return added, calls

    def memory_payload(self):
        a = self.message("我长期喜欢纯乐器音乐，平时自己找配乐听。你自己想听什么？")
        b = self.message("音乐我自己想先选一段配乐，仔细听配器的变化。", "out", ts=self.now + 1)
        return {"items": [{"who": str(self.HUMAN), "evidence": [a], "kind": "preference", "importance": 4,
                           "text": "小甲长期喜欢纯乐器音乐，平时自己找配乐听", "private": True}],
                "shared": [], "self_growth": [self.plan([a, b])]}

    def test_background_memory_call_learns_growth_and_keeps_fact_memory_in_one_request(self):
        added, calls = self.memory_review(self.memory_payload())
        self.assertEqual(1, added)
        self.assertEqual(1, len(calls))
        self.assertIn('"self_growth"', calls[0][2])
        self.assertEqual(1, len(self.items()))
        memories = self.store.list_person_memories(self.HUMAN, 20)
        self.assertEqual(1, len(memories))
        self.assertIn("长期喜欢", memories[0]["text"])
        self.assertEqual("private", memories[0]["visibility"])

    def test_background_opt_out_retains_original_fact_memory_without_growth(self):
        added, calls = self.memory_review(self.memory_payload(), enabled=False)
        self.assertEqual(1, added)
        self.assertEqual(1, len(calls))
        self.assertNotIn('"self_growth"', calls[0][2])
        self.assertEqual([], self.items())
        self.assertEqual(1, len(self.store.list_person_memories(self.HUMAN, 20)))

    def test_invalid_growth_from_same_model_does_not_drop_valid_fact_memory(self):
        payload = self.memory_payload()
        payload["self_growth"][0]["source_ids"] = [999999, 999998]
        added, calls = self.memory_review(payload)
        self.assertEqual(1, added)
        self.assertEqual(1, len(calls))
        self.assertEqual([], self.items())
        self.assertEqual(1, len(self.store.list_person_memories(self.HUMAN, 20)))

    def test_growth_commit_failure_does_not_drop_valid_fact_memory(self):
        payload = self.memory_payload()
        with patch.object(self.store, "commit_growth_review", side_effect=RuntimeError("fixture commit failure")):
            added, calls = self.memory_review(payload)
        self.assertEqual(1, added)
        self.assertEqual(1, len(calls))
        self.assertEqual([], self.items())
        self.assertEqual(1, len(self.store.list_person_memories(self.HUMAN, 20)))

    def test_growth_material_failure_does_not_add_another_model_call_or_drop_fact_memory(self):
        payload = self.memory_payload()
        with patch.object(self.store, "growth_material", side_effect=RuntimeError("fixture source failure")):
            added, calls = self.memory_review(payload)
        self.assertEqual(1, added)
        self.assertEqual(1, len(calls))
        self.assertNotIn('"self_growth"', calls[0][2])
        self.assertEqual([], self.items())
        self.assertEqual(1, len(self.store.list_person_memories(self.HUMAN, 20)))

    def test_unsaved_or_wrong_direction_reply_cannot_record_delivery_evidence(self):
        a = self.message("音乐你想听什么？")
        b = self.message("音乐我选配乐来听。", "out", ts=self.now + 1)
        self.assertFalse(self.store.growth_note_delivery(self.CONV, a, 999999))
        self.assertFalse(self.store.growth_note_delivery(self.CONV, a, a))
        self.assertFalse(self.store.growth_note_delivery(self.CONV, b, a))
        self.assertFalse(self.store.growth_note_delivery("g:202", a, b))
        self.assertEqual(0, self.store.conn.execute("SELECT count(*) FROM self_growth_turns").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
