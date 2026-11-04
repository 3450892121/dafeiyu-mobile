"""Explicit single cached-login action; all QQ/control calls are synthetic mocks."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from mobile_control import Control


class SavedLoginActionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.control=Control.__new__(Control)
        self.control.home=Path(self.temp.name)
        self.control.lock=threading.RLock()
        self.values={'login_uin':'190001','owner_qq':190002}
        self.control.cfg=Mock();self.control.cfg.get.side_effect=lambda key:self.values.get(key)
        self.control.status=Mock(return_value={'panel_ready':True,'qq_online':False,'connection_blocked':False})
        self.control.panel_request=Mock(return_value={'ok':True})
        self.runtime={'available':True,'online':False,'offline':False,'core_ready':False,
                      'phase':'waiting_qrcode','error_present':False,'verification_required':False}
        live=patch('mobile_session.login_runtime_status',side_effect=lambda *args:dict(self.runtime))
        saved=patch('mobile_session.saved_login_status',return_value={'available':True,'saved_account':True})
        self.live=live.start();self.saved=saved.start()
        self.addCleanup(live.stop);self.addCleanup(saved.stop)

    def test_explicit_action_checks_only_configured_small_account_and_never_changes_it(self):
        result=self.control.action('quick_login')
        self.assertTrue(result['submitted'])
        self.saved.assert_called_once_with(self.control.home,'190001',33112)
        self.control.panel_request.assert_called_once_with('/api/action',{'cmd':'quick_login'})
        self.control.cfg.set.assert_not_called()
        self.assertNotIn('已在线',result['message'])

    def test_concurrent_clicks_issue_only_one_login_request(self):
        answers=[]
        def click():
            try:answers.append(self.control.action('quick_login')['submitted'])
            except ValueError:answers.append(False)
        threads=[threading.Thread(target=click)for _ in range(8)]
        for worker in threads:worker.start()
        for worker in threads:worker.join()
        self.assertEqual(answers.count(True),1)
        self.control.panel_request.assert_called_once_with('/api/action',{'cmd':'quick_login'})

    def test_no_cached_target_never_falls_back_to_another_account(self):
        self.saved.return_value={'available':True,'saved_account':False}
        with self.assertRaises(ValueError):self.control.action('quick_login')
        self.control.panel_request.assert_not_called()

    def test_errors_verification_and_safety_pause_block_the_attempt(self):
        for key in ('error_present','verification_required'):
            with self.subTest(key=key):
                self.runtime[key]=True
                with self.assertRaises(ValueError):self.control.action('quick_login')
                self.runtime[key]=False
        self.control.status.return_value['connection_blocked']=True
        with self.assertRaises(ValueError):self.control.action('quick_login')
        self.control.panel_request.assert_not_called()

    def test_unknown_and_initializing_runtime_do_not_trigger_login(self):
        self.runtime['available']=False
        with self.assertRaises(ValueError):self.control.action('quick_login')
        self.runtime.update(available=True,phase='initializing')
        self.assertFalse(self.control.action('quick_login')['submitted'])
        self.control.panel_request.assert_not_called()

    def test_owner_account_is_never_used_as_bot(self):
        self.values['login_uin']='190002'
        with self.assertRaises(ValueError):self.control.action('quick_login')
        self.live.assert_not_called();self.control.panel_request.assert_not_called()

    def test_already_online_does_not_repeat_and_unconfirmed_identity_blocks(self):
        self.runtime['online']=True
        with self.assertRaises(ValueError):self.control.action('quick_login')
        self.control.status.return_value['qq_online']=True
        self.assertFalse(self.control.action('quick_login')['submitted'])
        self.control.panel_request.assert_not_called()

    def test_failed_or_timed_out_login_pauses_brain_and_never_retries(self):
        for failure in ({'ok':False},OSError('fictional failure with private upstream details')):
            with self.subTest(timeout=isinstance(failure,Exception)):
                self.control._saved_login_attempted=False
                self.control.panel_request.reset_mock()
                self.control.panel_request.side_effect=[failure,{'ok':True}]
                result=self.control.action('quick_login')
                self.assertFalse(result['submitted'])
                self.assertEqual(self.control.panel_request.call_args_list[-1].args,
                                 ('/api/action',{'cmd':'stop_brain'}))
                self.assertNotIn('private upstream details',str(result))
                with self.assertRaises(ValueError):self.control.action('quick_login')

    def test_only_confirmed_manual_stop_allows_a_new_attempt(self):
        self.control._saved_login_attempted=True
        self.control.panel_request.return_value={'ok':False}
        self.control.action('stop_all')
        self.assertTrue(self.control._saved_login_attempted)
        self.control.panel_request.return_value={'ok':True}
        self.control.action('stop_all')
        self.assertFalse(self.control._saved_login_attempted)


if __name__=='__main__':unittest.main()
