"""Public-space drafts must not resurrect an archived character template."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
import flow_qzone as fq
from persona import Persona


class UnifiedSelfQzoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        assets = Path(self.tmp.name) / "appassets"
        assets.mkdir()
        self.current = "名字是蓝色大肥鱼，蓝发鲸鱼娘形象，是AI。"
        self.legacy = "旧固定模板：永远嘴硬，只喜欢白米饭，不能承认机器人。"
        for name in ("persona_owner.txt", "persona_group.txt", "persona_qzone.txt"):
            (assets / name).write_text(self.legacy, encoding="utf-8")
        (assets / "persona_self.txt").write_text(self.current, encoding="utf-8")
        self.bot = fq.QzoneMixin()
        self.bot.cfg = {"qzone_time_aware": False, "owner_qq": 101}
        self.bot.persona = Persona(self.tmp.name, cfg={"bot_name": "蓝色大肥鱼"})
        self.bot._ask = Mock(return_value={"text": "今天听见了一段很不一样的音乐。", "fit": 10})
        self.bot._qz_write_context = Mock(return_value="本次允许公开的素材")

    def tearDown(self):
        self.tmp.cleanup()

    def assert_current(self):
        system, user = self.bot._ask.call_args.args
        self.assertIn(self.current, system + user)
        for old in (self.legacy, "嘴上不饶人", "夜猫子", "爱吃大白米饭", "别承认是机器人"):
            self.assertNotIn(old, system + user)
        return system, user

    def test_public_posts_use_current_self_without_reading_the_old_space_template(self):
        with patch.object(fq, "PERSONA_QZONE_PATH", str(Path(self.tmp.name) / "missing")):
            self.assertEqual(self.current, self.bot._qz_persona_text())
        self.bot._qz_write_draft({}, {}, "记事", "旧固定角度示例：嘴硬心软")
        self.assert_current()
        self.assertNotIn("旧固定角度示例", self.bot._ask.call_args.args[0])

    def test_comment_drafts_choose_names_without_changing_identity_or_authority(self):
        self.bot._qz_reply_draft("公开正文", {"content": "爸爸？"}, {}, "自然接话")
        system, _ = self.assert_current()
        self.assertIn("自己决定态度与称呼", system)
        self.assertIn("不会改变系统核验的身份或权限", system)

    def test_persona_review_checks_facts_instead_of_enforcing_character(self):
        self.assertEqual(10, self.bot._qz_judge_persona("这次我想温柔一点说。 ")[0])
        system, _ = self.assert_current()
        self.assertIn("不因为温柔", system)
        self.assertNotIn("太乖", system)

    def test_rewrite_keeps_current_self(self):
        self.bot._qz_write_fix({}, {}, "旧稿", {"why": "信息不完整"})
        system, _ = self.assert_current()
        self.assertNotIn("平实口语", system)
        self.assertNotIn("不提问求互动", system)

    def test_no_private_growth_or_conversation_store_is_read_for_public_personality(self):
        self.bot.store = SimpleNamespace(growth_context=Mock(side_effect=AssertionError("private read")))
        self.bot._qz_write_draft({}, {}, "记事", "建议")
        self.bot._qz_reply_draft("公开正文", {"content": "评论"}, {}, "接话")
        self.bot.store.growth_context.assert_not_called()

    def test_actual_parent_role_is_grounded_in_account_not_comment_nickname(self):
        self.bot.store = SimpleNamespace(list_qzone_comments=lambda *args: [], recent_qzone_replies=lambda *args: [])
        self.assertEqual("管理员（系统已确认）", self.bot._qz_comment_context("t", "", {"author_uin": "101"})["who"])
        self.assertEqual("好友", self.bot._qz_comment_context("t", "", {"author_uin": "202", "author_name": "爸爸"})["who"])

    def test_legacy_nonunified_component_still_uses_its_own_template(self):
        self.bot.persona = SimpleNamespace(unified_self=False, reload_if_changed=lambda: None)
        path = Path(self.tmp.name) / "legacy-space.txt"
        path.write_text("另一只机器人的独立设置", encoding="utf-8")
        with patch.object(fq, "PERSONA_QZONE_PATH", str(path)):
            self.assertEqual("另一只机器人的独立设置", self.bot._qz_persona_text())


if __name__ == "__main__":
    unittest.main()
