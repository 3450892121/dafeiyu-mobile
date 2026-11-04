"""The group auto-mention switch also applies to reminders, without dropping reminders."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from main import Bot
from onebot import text_seg


class ReminderMentionTests(unittest.TestCase):
    def bot(self,enabled=True,conv='g:202'):
        bot=Bot.__new__(Bot)
        bot.cfg={'bot_name':'纳西妲','trigger_name':'小妲','group_at_reply':enabled,
                 'group_at_reply_at':True,'group_at_reply_name':True,'group_at_reply_quote':True}
        bot.self_id=99
        bot._reminder_fail={};bot._reminder_giveup=set()
        self.reminder={'id':1,'conv':conv,'text':'朋友原话：@小甲 记得带书。','target_uid':101,'target_name':'小甲'}
        bot.store=SimpleNamespace(due_reminders=lambda:[self.reminder],mark_reminder=Mock(),
                                  add_message=Mock(),touch_session=Mock())
        bot._reminder_line=Mock(return_value=self.reminder['text'])
        bot._send=Mock(return_value=True)
        return bot

    def test_disabling_total_switch_keeps_reminder_text_and_confirmation_without_at(self):
        for setting in (False,0,'false','off'):
            with self.subTest(setting=setting):
                bot=self.bot(setting)
                bot._deliver_due_reminders()
                bot._send.assert_called_once_with(True,202,0,[text_seg(self.reminder['text'])])
                bot.store.mark_reminder.assert_called_once_with(1)
                self.assertEqual(self.reminder['target_uid'],101)
                self.assertIn('@小甲',bot.store.add_message.call_args.args[4])

    def test_enabled_total_switch_preserves_targeted_group_reminder(self):
        bot=self.bot(True)
        bot._deliver_due_reminders()
        segments=bot._send.call_args.args[3]
        self.assertEqual(segments[0],{'type':'at','data':{'qq':'101'}})
        self.assertEqual(segments[1],text_seg(self.reminder['text']))
        bot.store.mark_reminder.assert_called_once_with(1)

    def test_setting_is_checked_after_drafting_and_before_automatic_at_is_added(self):
        bot=self.bot(True)
        def draft(reminder):
            bot.cfg['group_at_reply']=False
            return reminder['text']
        bot._reminder_line.side_effect=draft
        bot._deliver_due_reminders()
        self.assertEqual(bot._send.call_args.args[3],[text_seg(self.reminder['text'])])

    def test_private_reminder_never_adds_a_group_mention(self):
        for enabled in (True,False):
            bot=self.bot(enabled,conv='p:101')
            bot._deliver_due_reminders()
            bot._send.assert_called_once_with(False,0,101,[text_seg(self.reminder['text'])])

    def test_outgoing_switch_does_not_disable_receiving_an_at_to_this_account(self):
        bot=self.bot(False)
        bot._self_qq=Mock(return_value=99)
        addressed,_=bot._addressed([{'type':'at','data':{'qq':'99'}},text_seg('你好')],
                                   '你好',is_group=True)
        self.assertTrue(addressed)


if __name__=='__main__':unittest.main()
