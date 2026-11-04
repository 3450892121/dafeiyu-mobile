"""Image and public-space prompts follow this instance's character, using fake models only."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import capabilities
from flow_media import MediaMixin, draw_self_hint
from flow_qzone import QzoneMixin


class CharacterMediaPromptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.getcwd())
        self.addCleanup(self.temp.cleanup)
        self.assets=Path(self.temp.name)/'appassets';self.assets.mkdir()
        self.cfg={'bot_name':'纳西妲','trigger_name':'小妲','qzone_time_aware':False,'owner_qq':190002}
        self.current='你是纳西妲。\n- 形象：浅色头发、绿色眼睛、白绿衣饰。\n- 管理员的眼睛：虚构私人细节。'

    def media(self):
        bot=MediaMixin();bot.cfg=self.cfg
        bot.persona=SimpleNamespace(self_text=self.current,identity=self.current,assets=str(self.assets),
                                    reload_if_changed=Mock())
        bot.pool=SimpleNamespace(chat=Mock(return_value=SimpleNamespace(ok=False,text='')))
        return bot

    def test_self_portrait_model_and_fallback_use_current_appearance_without_private_notes(self):
        bot=self.media()
        fallback=bot._draw_prompt_text('画你自己看书','画你自己看书','',True)
        system,history,user=bot.pool.chat.call_args.args
        for text in (user,fallback):
            self.assertIn('纳西妲',text)
            self.assertIn('白绿衣饰',text)
            self.assertNotIn('虚构私人细节',text)
            for old in ('大肥鱼','鲸尾','鲸鱼娘','本鲸'):
                self.assertNotIn(old,text)
        bot.persona.reload_if_changed.assert_called()

    def test_missing_appearance_and_another_character_never_restore_reference_identity(self):
        bot=self.media();bot.cfg={'bot_name':'夏铃'}
        bot.persona.self_text='当前身份是夏铃。';bot.persona.identity=''
        prompt=bot._draw_prompt_text('自画像','','',True)
        self.assertIn('夏铃',prompt)
        self.assertNotIn('纳西妲',prompt)
        self.assertNotIn('大肥鱼',prompt)
        self.assertNotIn('鲸鱼娘',prompt)

    def test_only_explicit_instance_self_avatar_is_automatically_used(self):
        bot=self.media()
        (self.assets/'avatar-src.jpg').write_bytes(b'old-unselected-avatar')
        self.assertEqual('',bot._draw_self_avatar())
        selected=self.assets/'self-avatar.png';selected.write_bytes(b'explicit-instance-avatar')
        self.assertEqual(str(selected),bot._draw_self_avatar())

    def test_drawing_another_named_character_is_not_rewritten_into_self(self):
        bot=self.media()
        prompt=bot._draw_prompt_text('画大肥鱼的画像','画大肥鱼的画像','',False)
        self.assertIn('画大肥鱼的画像',prompt)
        self.assertNotIn('纳西妲',prompt)
        self.assertFalse(draw_self_hint('画大肥鱼的画像'))
        self.assertFalse(draw_self_hint('画一只鲸鱼'))
        self.assertFalse(draw_self_hint('朋友说本鲸是他的自称'))
        self.assertTrue(draw_self_hint('请画你自己看书'))

    def qzone(self,unified):
        bot=QzoneMixin();bot.cfg=dict(self.cfg)
        bot.persona=SimpleNamespace(unified_self=unified)
        bot._qz_persona_text=Mock(return_value='当前角色纳西妲；白绿衣饰，基于真实相处形成经历。')
        bot._qz_examples=Mock(return_value=([],[]))
        bot._ask=Mock(return_value={'worth':False,'text':'今天看到了一本有意思的书。','fit':10,'best':1})
        return bot

    def test_all_public_drafting_and_selection_branches_follow_current_character(self):
        for unified in (False,True):
            with self.subTest(unified=unified):
                bot=self.qzone(unified)
                bot._qz_decide({})
                bot._qz_write_draft({}, {},'记事','自然记录')
                bot._qz_rank([{'text':'今天看到了一本有意思的书。'}])
                bot._qz_judge_persona('今天看到了一本有意思的书。')
                bot._qz_write_fix({}, {},'旧稿',{'why':'不够清楚'})
                bot._qz_reply_draft('公开正文',{'content':'这本书怎么样？'}, {},'自然接话')
                self.assertEqual(6,bot._ask.call_count)
                all_prompts='\n'.join(system+'\n'+user for (system,user),_ in bot._ask.call_args_list)
                self.assertIn('纳西妲',all_prompts)
                for old in ('蓝色大肥鱼','鲸鱼娘','本鲸','爱吃大白米饭','嘴上不饶人'):
                    self.assertNotIn(old,all_prompts)

    def test_public_historical_quotes_are_preserved_while_current_identity_is_separate(self):
        bot=self.qzone(True)
        original='朋友原话：大肥鱼今天没来。'
        bot._qz_reply_draft(original,{'content':'刚才说的是什么？'}, {},'自然接话')
        system,user=bot._ask.call_args.args
        self.assertIn(original,user)
        self.assertIn('纳西妲',system)
        self.assertNotIn('蓝色大肥鱼',system)

    def test_legacy_space_persona_reads_its_own_directory_without_shared_identity_fallback(self):
        bot=QzoneMixin();bot.cfg=dict(self.cfg)
        bot.persona=SimpleNamespace(unified_self=False,assets=str(self.assets),
                                    identity='本实例的纳西妲身份',reload_if_changed=Mock())
        self.assertEqual('本实例的纳西妲身份',bot._qz_persona_text())
        (self.assets/'persona_qzone.txt').write_text('本实例独立的公开资料',encoding='utf-8')
        self.assertEqual('本实例独立的公开资料',bot._qz_persona_text())

    def test_public_name_filter_exempts_current_names_instead_of_reference_alias(self):
        bot=self.qzone(True)
        ok,reason,_=bot._qz_draft_check('纳西妲今天整理好了一份读书笔记。',[],70,names=['纳西妲'])
        self.assertTrue(ok,reason)
        ok,reason,_=bot._qz_draft_check('大肥鱼今天整理好了一份读书笔记。',[],70,names=['大肥鱼'])
        self.assertFalse(ok)
        self.assertIn('含好友昵称',reason)

    def test_verified_memory_relation_uses_only_configured_identity(self):
        note=capabilities.memory_relation_note(self.cfg)
        self.assertIn('纳西妲',note)
        self.assertIn('账号 190002',note)
        self.assertNotIn('大肥鱼',note)
        self.assertNotIn('纳西妲',capabilities.memory_relation_note({'owner_qq':190002,'bot_name':'夏铃'}))
        self.assertEqual('',capabilities.memory_relation_note({'bot_name':'纳西妲'}))


if __name__=='__main__':unittest.main()
