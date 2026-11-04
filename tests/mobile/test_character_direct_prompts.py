"""Generated intent and direct fallback responses do not borrow another character."""
from types import SimpleNamespace
import unittest

import care
import refuse
from flow_intent import IntentMixin


class CharacterDirectPromptTests(unittest.TestCase):
    def test_intent_fallback_uses_current_configuration_and_keeps_real_history(self):
        bot=IntentMixin();bot.cfg={'bot_name':'纳西妲','trigger_name':'小妲'}
        history=[{'role':'user','content':'[朋友] 大肥鱼今天说他喜欢这个。'}]
        prompt=bot._intent_judge_prompt('小妲你怎么看？',history,'测试好友',True)
        self.assertIn('你是 QQ 机器人「纳西妲」',prompt)
        self.assertIn(history[0]['content'],prompt)
        self.assertNotIn('你是 QQ 机器人「蓝色大肥鱼」',prompt)
        bot.cfg['bot_name']='测试角色乙'
        prompt=bot._intent_judge_prompt('你怎么看？',[],'测试好友',False)
        self.assertIn('你是 QQ 机器人「测试角色乙」',prompt)
        self.assertNotIn('纳西妲',prompt)

    def test_generic_intent_fallback_does_not_preselect_any_character(self):
        prompt=IntentMixin()._intent_judge_prompt('这个什么意思？',[],'测试好友',False)
        self.assertIn('你是 QQ 机器人「机器人」',prompt)
        for name in ('纳西妲','大肥鱼','本鲸'):self.assertNotIn(name,prompt)

    def test_unmatched_character_care_uses_supportive_neutral_lines(self):
        for name in ('纳西妲','测试角色乙',''):
            for line in care.lines_for(name):
                for old in ('大肥鱼','本鲸','使唤','来跟我横','少来这套'):
                    self.assertNotIn(old,line)
                self.assertTrue(line.strip())
        self.assertNotIn('纳西妲',care.care_line('测试角色乙','p:fictional'))

    def test_unmatched_character_refusals_and_reminders_are_neutral(self):
        saved={}
        store=SimpleNamespace(get_setting=lambda key:saved.get(key),set_setting=saved.__setitem__)
        for kind in ('office','qzone','billing','reminder_full','unknown'):
            for _ in range(3):
                text=refuse.line('纳西妲',kind,'p:fictional',store)
                for old in ('大肥鱼','本鲸','主人','纳西妲'):
                    self.assertNotIn(old,text)
        ack=refuse.reminder_ack('纳西妲','明天9点')
        self.assertIn('明天9点',ack)
        self.assertIn('提醒',ack)
        self.assertNotIn('本鲸',ack)


if __name__=='__main__':unittest.main()
