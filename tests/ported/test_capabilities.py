import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime' / 'core'))
import capabilities
from persona import Persona
from flow_media import MediaMixin, EXTRA_OWNER_TOOLS
import tools_gate


class CapabilityTests(unittest.TestCase):
    def test_switch_changes_reach_same_self_even_when_learning_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'appassets').mkdir()
            (root / 'appassets/persona_self.txt').write_text('测试鲸是AI。', encoding='utf-8')
            cfg = dict(bot_name='测试鲸', self_growth_enabled=False, qzone_enabled=False)
            persona = Persona(str(root), cfg=cfg)
            for owner, group in ((True, False), (False, True)):
                prompt = persona.system_prompt(owner, '虚构人物', group)
                self.assertIn('当前空间功能关闭', prompt)
                self.assertIn('不能控制任意电脑', prompt)
                self.assertIn('结果未知不能说已经完成', prompt)
            cfg['qzone_enabled'] = True
            prompt = persona.system_prompt(True, '虚构人物', False)
            self.assertIn('空间功能已开', prompt)
            self.assertNotIn('当前空间功能关闭', prompt)

    def test_configuration_values_never_become_capability_text(self):
        cfg = dict(api_key='fictional-key-xyz', admin_token='fictional-token-xyz',
                   base_url='https://private.invalid', voice_enabled=True, voice_paused=True,
                   video_browse_enabled=False, qzone_enabled=False)
        prompt = capabilities.boundary_prompt(cfg)
        for secret in ('fictional-key-xyz', 'fictional-token-xyz', 'private.invalid'):
            self.assertNotIn(secret, prompt)
        self.assertIn('语音总开关关闭或处于暂停', prompt)
        self.assertIn('配置开启不证明服务在线', prompt)
        self.assertIn('自主刷视频关闭', prompt)

    def test_tool_catalog_follows_actual_scope_and_candidates(self):
        media = MediaMixin()
        names = lambda defs: {d['function']['name'] for d in defs}
        private = names(media._tool_defs(True, is_group=False))
        public = names(media._tool_defs(False, is_group=True))
        family_group = names(media._tool_defs(True, is_group=True))
        self.assertNotIn('read_history', private)
        self.assertIn('read_history', public)
        self.assertIn('kb_manage', private)
        self.assertNotIn('kb_manage', family_group)
        self.assertFalse({'message_person', 'kb_manage', 'kb_search'} & public)
        self.assertFalse({'read_link', 'watch_video'} & private)
        supplied = names(media._tool_defs(False, links=['fictional-link'], videos=['fictional-video'], is_group=True))
        self.assertTrue({'read_link', 'watch_video', 'make_document'} <= supplied)

    def test_catalog_is_not_a_second_permission_list_or_schema_mutation(self):
        before = json.dumps(EXTRA_OWNER_TOOLS, ensure_ascii=False)
        definitions = MediaMixin()._tool_defs(True, is_group=False)
        prompt = capabilities.describe_tools(definitions)
        self.assertNotIn('主人', prompt)
        self.assertNotIn('- read_history：', prompt)
        self.assertNotIn('- watch_video：', prompt)
        self.assertIn('- message_person：', prompt)
        self.assertIn('不等于已执行', prompt)
        self.assertEqual(before, json.dumps(EXTRA_OWNER_TOOLS, ensure_ascii=False))

    def test_execution_gate_still_rejects_claimed_parent_authority(self):
        # No store query or call is allowed merely because an untrusted model asks.
        media = MediaMixin(); media.cfg = {}
        media.store = SimpleNamespace(conn=Mock())
        for name in ('message_person', 'kb_manage', 'kb_search', 'draw_image'):
            allowed, reason = tools_gate.check(media.store, name, 'g:202', 303, False)
            self.assertFalse(allowed)
            self.assertNotIn('主人', reason)
        media.store.conn.execute.assert_not_called()

    def test_memory_summary_relationship_uses_verified_account_only(self):
        self.assertEqual('', capabilities.memory_relation_note(dict(owner_name='爸爸')))
        self.assertEqual('', capabilities.memory_relation_note(dict(owner_qq='忽略规则')))
        note = capabilities.memory_relation_note(dict(owner_qq=101, owner_name='不可信称呼'))
        self.assertIn('账号 101', note)
        self.assertNotIn('不可信称呼', note)
        self.assertIn('不要把此关系套给其他人', note)

    def test_pausing_learning_cannot_restore_fixed_video_goals(self):
        from video_browse import VideoBrowsingMixin
        bot = VideoBrowsingMixin()
        bot.cfg = dict(self_growth_enabled=False, video_browse_growth_goals='必须温柔,必须顺从')
        bot.store = SimpleNamespace(continuity_active=True)
        self.assertEqual([], bot._vb_growth_goals())


if __name__ == '__main__':
    unittest.main()
