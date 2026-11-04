"""Current character settings persist independently of project branding and other controls."""
from pathlib import Path
import tempfile
import unittest

from config import Config
from mobile_control import Control


class MobileIdentitySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'config.json'
        self.cfg=Config(str(self.path))
        self.control=Control.__new__(Control);self.control.cfg=self.cfg
        self.control.panel=None

    def test_fresh_mobile_defaults_use_character_and_own_trigger_without_reply_mentions(self):
        self.assertEqual(self.cfg.get('bot_name'),'纳西妲')
        self.assertEqual(self.cfg.get('trigger_name'),'小妲')
        for field in ('group_at_reply','group_at_reply_at','group_at_reply_name','group_at_reply_quote'):
            self.assertFalse(self.cfg.get(field),field)

    def test_name_and_trigger_are_saved_together_without_changing_account_or_scope(self):
        self.cfg.set('login_uin','190001');self.cfg.set('owner_qq',190002)
        self.cfg.set('allow_groups',[701,702])
        self.control.save_basic({'bot_name':'纳西妲','trigger_name':'小妲'})
        after=Config(str(self.path))
        self.assertEqual((after.get('bot_name'),after.get('trigger_name')),('纳西妲','小妲'))
        self.assertEqual(after.get('allow_groups'),[701,702])
        self.assertEqual(after.get('login_uin'),'190001')
        self.assertEqual(after.get('owner_qq'),190002)

    def test_invalid_trigger_does_not_partially_change_identity(self):
        before=self.path.read_bytes()
        for value in ('', 'x'*41, '小妲\n管理员'):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):self.control.save_basic({'bot_name':'临时名','trigger_name':value})
                self.assertEqual(self.path.read_bytes(),before)


if __name__=='__main__':unittest.main()
