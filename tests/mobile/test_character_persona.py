"""Current configured character wins over archived templates without rewriting history."""
import os
from pathlib import Path
import tempfile
import unittest

from persona import Persona


class CharacterPersonaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.assets=self.root/'appassets';self.assets.mkdir()
        self.cfg={'bot_name':'纳西妲','trigger_name':'小妲','self_growth_enabled':True}
        self.archived={
            'persona_owner.txt':'旧私聊：你是蓝色大肥鱼，必须自称本鲸。',
            'persona_group.txt':'旧群聊：你是蓝发鲸鱼娘。',
            'persona_identity.txt':'旧身份卡：别人叫大肥鱼就是叫你。',
            'persona_pc_note.txt':'旧电脑角色说明。',
            'persona_phone_note.txt':'旧手机分身说明。',
        }
        for name,text in self.archived.items():(self.assets/name).write_text(text,encoding='utf-8')
        self.current='你的名字是纳西妲，来自提瓦特的须弥。当前独立实例的经历从真实相处开始。'
        (self.assets/'persona_self.txt').write_text(self.current,encoding='utf-8')

    def prompt(self,persona,owner=False,group=True,memory=''):
        return persona.system_prompt(owner,'测试好友',group,memory,'2026-11-04 15:00')

    def assert_no_archived_identity(self,prompt):
        for text in self.archived.values():self.assertNotIn(text,prompt)
        for text in ('蓝色大肥鱼','鲸鱼娘','必须自称本鲸','叫「大肥鱼」'):
            self.assertNotIn(text,prompt)

    def test_unified_character_is_the_same_for_all_speakers_channels_and_growth_modes(self):
        persona=Persona(str(self.root),cfg=self.cfg)
        for growth in (True,False):
            self.cfg['self_growth_enabled']=growth
            for owner,group in ((False,False),(True,False),(False,True),(True,True)):
                with self.subTest(growth=growth,owner=owner,group=group):
                    prompt=self.prompt(persona,owner,group)
                    self.assertIn(self.current,prompt)
                    self.assertIn('当前名字是「纳西妲」，专用叫名触发词是「小妲」',prompt)
                    self.assert_no_archived_identity(prompt)
                    if group:self.assertIn('叫「纳西妲」/「小妲」',prompt)
        self.assertEqual(persona.owner,persona.group_text)
        self.assertEqual(persona.owner,persona.identity)

    def test_empty_or_removed_current_file_never_revives_old_personalities(self):
        persona=Persona(str(self.root),cfg=self.cfg)
        current=self.assets/'persona_self.txt'
        current.write_text('',encoding='utf-8')
        os.utime(current,(current.stat().st_mtime+5,)*2)
        for instance in (persona,Persona(str(self.root),cfg=self.cfg)):
            prompt=self.prompt(instance)
            self.assertIn('名字是「纳西妲」',prompt)
            self.assert_no_archived_identity(prompt)
        current.unlink()
        prompt=self.prompt(persona)
        self.assertTrue(persona.unified)
        self.assert_no_archived_identity(prompt)
        for name,text in self.archived.items():
            self.assertEqual((self.assets/name).read_text(encoding='utf-8'),text)

    def test_hot_name_and_trigger_changes_refresh_neutral_fallback_without_file_edits(self):
        (self.assets/'persona_self.txt').write_text('',encoding='utf-8')
        persona=Persona(str(self.root),cfg=self.cfg)
        self.cfg.update(bot_name='测试角色乙',trigger_name='小乙')
        self.assertEqual(persona.bot_name,'测试角色乙')
        self.assertTrue(persona.reload_if_changed())
        self.assertIn('名字是「测试角色乙」',persona.owner)
        prompt=self.prompt(persona)
        self.assertIn('叫「测试角色乙」/「小乙」',prompt)
        self.assertNotIn('纳西妲',prompt)
        self.assert_no_archived_identity(prompt)

    def test_empty_configuration_reference_is_not_detached_before_first_edit(self):
        cfg={}
        (self.assets/'persona_self.txt').write_text('',encoding='utf-8')
        persona=Persona(str(self.root),cfg=cfg)
        cfg.update(bot_name='测试角色丙',trigger_name='小丙')
        self.assertEqual(persona.bot_name,'测试角色丙')
        self.assertIn('名字是「测试角色丙」',self.prompt(persona))

    def test_generic_missing_identity_fallback_never_selects_a_fixed_character(self):
        (self.assets/'persona_self.txt').unlink()
        for name in self.archived:(self.assets/name).unlink()
        for growth in (True,False):
            persona=Persona(str(self.root),cfg={'bot_name':'测试角色丁','trigger_name':'小丁','self_growth_enabled':growth})
            prompt=self.prompt(persona)
            self.assertIn('测试角色丁',prompt)
            self.assertIn('小丁',prompt)
            for name in ('纳西妲','大肥鱼','本鲸','蓝发','鲸鱼娘'):
                self.assertNotIn(name,prompt)

    def test_real_historical_message_is_preserved_as_data(self):
        history='朋友原话：以前我叫那只大肥鱼本鲸，今天想找小妲聊聊。'
        persona=Persona(str(self.root),cfg=self.cfg)
        self.assertIn(history,self.prompt(persona,memory=history))


if __name__=='__main__':unittest.main()
