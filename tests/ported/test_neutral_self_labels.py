"""Role metadata must not prescribe a nickname or rewrite authentic speech."""
import ast
import contextlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import recent_topic

import common
from flow_context import BotVoiceNoteMixin
from flow_intent import IntentMixin
from flow_memory import MemoryMixin
from flow_proactive import ProactiveMixin
from flow_reply import ReplyMixin
from persona import Persona
import style


class NeutralSelfLabelsTests(unittest.TestCase):
    def test_generated_identity_note_uses_account_and_role_without_fixed_self_name(self):
        note=common.bot_identity_note(99,'蓝色大肥鱼','')
        self.assertIn('蓝色大肥鱼',note)
        self.assertIn('QQ99',note)
        self.assertNotIn('本鲸',note)
        self.assertIn('不是名字、自称或昵称',note)

    def test_classifier_labels_are_neutral_while_speaker_body_is_untouched(self):
        bot=BotVoiceNoteMixin();bot.cfg={}
        bot.store=SimpleNamespace(messages_for_context=lambda *a:[
            {'role':'assistant','content':'本鲸想歇会儿。'},
            {'role':'user','content':'[本鲸(QQ42)] 我也用本鲸这个自称。'}])
        rows=bot._judge_history_lines('g:7')
        self.assertEqual(rows[0],'- 本机器人（你）：本鲸想歇会儿。')
        self.assertIn('本鲸(QQ42)',rows[1])
        self.assertIn('我也用本鲸这个自称',rows[1])

    def test_intent_prompt_does_not_turn_role_into_whale_nickname(self):
        bot=IntentMixin();bot._bot_name=lambda:'蓝色大肥鱼'
        prompt=bot._intent_judge_prompt('刚才那个是什么意思',[],'甲',True)
        self.assertIn('QQ 机器人「蓝色大肥鱼」',prompt)
        self.assertNotIn('本鲸',prompt)

    def test_roster_neutralizes_only_the_exact_old_generated_rule(self):
        bot=ReplyMixin();bot.cfg={};bot._self_qq=lambda:99;bot._bot_names=lambda:['蓝色大肥鱼']
        bot.store=SimpleNamespace(same_name_people=lambda *a:[])
        old='哦鲸鲸(QQ88)不是本鲸：训他/骂他/夸他都不算本鲸的事'
        quoted='朋友原话：我就爱叫自己本鲸。'
        bot._identity_corrections=lambda:[old,quoted]
        note=bot._roster_note()
        self.assertIn('你的 QQ 号是 99',note)
        self.assertIn('哦鲸鲸(QQ88)不是你：训他/骂他/夸他都不算你的事',note)
        self.assertIn(quoted,note)
        self.assertEqual(bot._identity_corrections(),[old,quoted])

    def test_actual_normal_reply_account_block_does_not_define_a_fixed_self_name(self):
        source=Path(common.__file__).with_name('flow_reply.py')
        tree=ast.parse(source.read_text(encoding='utf-8-sig'))
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_reply')
        block=next(n for n in method.body if isinstance(n,ast.Try) and
                   any(isinstance(x,ast.Constant) and isinstance(x.value,str) and '# 现在谁在说' in x.value for x in ast.walk(n)))
        bot=SimpleNamespace(_person_name=lambda *a:'甲',_self_qq=lambda:99,_bot_name=lambda:'蓝色大肥鱼')
        env=dict(self=bot,uid=42,name='甲',is_owner=False,system='',mention_note='',_as_int=int,log=Mock())
        exec(compile(ast.Module(body=[block],type_ignores=[]),'<actual current speaker block>','exec'),env)
        self.assertIn('你的 QQ 是 99',env['system'])
        self.assertNotIn('本鲸',env['system'])

    def test_profile_transcript_preserves_real_content_with_neutral_metadata(self):
        bot=MemoryMixin();bot._canonical_name=lambda uid,name:name
        history=[{'direction':'out','sender_id':99,'text':'本鲸刚才只是开玩笑。'},
                 {'direction':'in','sender_id':42,'sender_name':'甲','text':'我的昵称就是本鲸。'}]
        transcript=bot._card_history_lines(42,'甲',history)
        self.assertIn('本机器人（你）：本鲸刚才只是开玩笑。',transcript)
        self.assertIn('甲(QQ42)：我的昵称就是本鲸。',transcript)
        self.assertEqual(history[0]['text'],'本鲸刚才只是开玩笑。')

    def test_new_and_legacy_share_role_labels_are_accepted_without_accepting_a_bare_alias(self):
        for marker in ('本机器人（你）','本鲸（你，机器人）'):
            self.assertTrue(ProactiveMixin._share_attribution({'actor':'self'},marker+'：合成经历')[0])
        self.assertFalse(ProactiveMixin._share_attribution({'actor':'self'},'甲：我也叫本鲸。')[0])
        self.assertFalse(ProactiveMixin._share_attribution({'actor':'other'},'本机器人（你）：合成经历')[0])

    def test_unified_persona_distinguishes_name_self_reference_and_calling_name(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as fixture:
            assets=Path(fixture)/'appassets';assets.mkdir()
            (assets/'persona_self.txt').write_text('你的名字是蓝色大肥鱼。',encoding='utf-8')
            (assets/'persona_owner.txt').write_text('旧模板：必须自称本鲸大人。',encoding='utf-8')
            p=Persona(fixture,cfg={'bot_name':'蓝色大肥鱼','self_growth_enabled':True})
            prompt=p.system_prompt(False,'甲',True,now_line='2026-11-03 17:00')
            self.assertNotIn('本鲸',prompt)
            self.assertIn('名字、你对自己的自称、别人叫你的称呼',prompt)
            self.assertIn('自己选择其他表达',prompt)
            self.assertEqual((assets/'persona_owner.txt').read_text(encoding='utf-8'),'旧模板：必须自称本鲸大人。')

    def test_natural_self_references_and_authentic_names_are_not_replaced_or_banned(self):
        for text in ('我叫蓝色大肥鱼。','本鲸今天想歇会儿。','别人真的叫我本鲸。','你要叫我本鲸也成。'):
            self.assertEqual(style.clean(text,self_growth_enabled=True),text)

    def test_topic_summary_receives_neutral_header_and_authentic_message_body(self):
        summarize=Mock(return_value='合成话题')
        store=SimpleNamespace(get_setting=lambda *a:'',_lock=contextlib.nullcontext(),
            conn=SimpleNamespace(execute=lambda *a:SimpleNamespace(fetchall=lambda:[(4,),(3,),(2,),(1,)])),
            messages_for_context=lambda *a,**k:[{'role':'assistant','content':'本鲸刚才聊过这个。'}],
            person_name=lambda *a:'甲',set_setting=Mock())
        bot=SimpleNamespace(store=store,pool=SimpleNamespace(summarize_recent_topic=summarize),
            _self_qq=lambda:99,_bot_names=lambda:['蓝色大肥鱼'],summarizing=set(),summarizing_lock=contextlib.nullcontext())
        class Immediate:
            def __init__(self,target,**kwargs):self.target=target
            def start(self):self.target()
        with patch.object(recent_topic.threading,'Thread',Immediate):
            recent_topic.schedule(bot,'p:42')
        summarize.assert_called_once_with('本机器人（你）：本鲸刚才聊过这个。')


if __name__=='__main__':unittest.main()
