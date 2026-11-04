"""Both management entry points share a single guarded login attempt; no live QQ."""
import threading
import unittest
from unittest.mock import Mock, patch

from mobile_console import MobileSupervisor, _BaseSupervisor


class SavedLoginSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.supervisor=MobileSupervisor.__new__(MobileSupervisor)
        self.supervisor.lock=threading.RLock()
        self.values={'login_uin':'190001','owner_qq':190002,'mobile_autostart':False}
        self.supervisor.cfg=Mock()
        self.supervisor.cfg.get.side_effect=self.values.get
        self.supervisor.cfg.set.side_effect=self.values.__setitem__
        self.supervisor._connection_blocked=False
        self.supervisor._saved_login_attempted=False
        self.supervisor._brain_process=Mock()
        self.supervisor._qq_process=Mock()
        self.supervisor._stop=Mock(return_value=True)
        self.supervisor._log_console=Mock()
        self.supervisor.user_stopped=True
        self.current={'available':True,'online':False,'phase':'waiting_qrcode',
                      'error_present':False,'verification_required':False}
        self.saved={'available':True,'saved_account':True}
        runtime=patch('mobile_session.login_runtime_status',side_effect=lambda *args:dict(self.current))
        saved=patch('mobile_session.saved_login_status',side_effect=lambda *args:dict(self.saved))
        submit=patch.object(_BaseSupervisor,'quick_login',return_value=True)
        self.runtime=runtime.start();self.probe=saved.start();self.submit=submit.start()
        for item in (runtime,saved,submit):self.addCleanup(item.stop)

    def test_full_panel_and_direct_calls_share_concurrent_attempt_limit(self):
        answers=[]
        workers=[threading.Thread(target=lambda:answers.append(self.supervisor.action('quick_login')))for _ in range(8)]
        for worker in workers:worker.start()
        for worker in workers:worker.join()
        self.assertEqual(answers.count(True),1)
        self.assertFalse(self.supervisor.quick_login())
        self.submit.assert_called_once_with()
        self.assertEqual(self.values['login_uin'],'190001')
        self.assertTrue(self.values['mobile_autostart'])

    def test_safety_pause_is_not_cleared_by_full_panel_action(self):
        self.supervisor._connection_blocked=True
        self.assertFalse(self.supervisor.action('quick_login'))
        self.assertTrue(self.supervisor._connection_blocked)
        self.assertFalse(self.values['mobile_autostart'])
        self.runtime.assert_not_called();self.submit.assert_not_called()

    def test_missing_cache_or_unavailable_runtime_never_tries_another_account(self):
        self.saved['saved_account']=False
        self.assertFalse(self.supervisor.action('quick_login'))
        self.current['available']=False
        self.assertFalse(self.supervisor.action('quick_login'))
        self.submit.assert_not_called()
        self.assertFalse(self.supervisor._saved_login_attempted)

    def test_verification_blocks_and_persists_pause_before_submission(self):
        self.current['verification_required']=True
        self.assertFalse(self.supervisor.action('quick_login'))
        self.assertTrue(self.supervisor._connection_blocked)
        self.assertFalse(self.values['mobile_autostart'])
        self.supervisor._stop.assert_called_once_with(self.supervisor._brain_process)
        self.submit.assert_not_called()

    def test_failed_or_uncertain_attempt_persists_pause_without_retry(self):
        for failure in (False,OSError('synthetic private upstream detail')):
            with self.subTest(failure=type(failure).__name__):
                self.supervisor._connection_blocked=False
                self.supervisor._saved_login_attempted=False
                self.submit.reset_mock()
                self.submit.side_effect=failure if isinstance(failure,Exception) else None
                self.submit.return_value=False
                self.assertFalse(self.supervisor.action('quick_login'))
                self.assertTrue(self.supervisor._connection_blocked)
                self.assertTrue(self.supervisor._saved_login_attempted)
                self.assertFalse(self.supervisor.quick_login())
                self.submit.assert_called_once_with()
                self.assertNotIn('private upstream',str(self.supervisor._log_console.call_args))

    def test_only_successful_qq_stop_resets_the_attempt(self):
        self.supervisor._saved_login_attempted=True
        self.supervisor._stop.return_value=False
        self.assertFalse(self.supervisor.stop_napcat())
        self.assertTrue(self.supervisor._saved_login_attempted)
        self.supervisor._stop.return_value=True
        self.assertTrue(self.supervisor.stop_napcat())
        self.assertFalse(self.supervisor._saved_login_attempted)

    def test_online_requires_confirmed_identity_and_never_relogs(self):
        self.current['online']=True
        self.supervisor.login_info=Mock(return_value=None)
        self.assertFalse(self.supervisor.action('quick_login'))
        self.supervisor.login_info.return_value={'user_id':190001}
        self.assertTrue(self.supervisor.action('quick_login'))
        self.submit.assert_not_called();self.probe.assert_not_called()

    def test_safety_pause_during_submission_cannot_reenable_restart(self):
        def restrict():
            self.supervisor.block_connection('synthetic verification')
            return True
        self.submit.side_effect=restrict
        self.assertFalse(self.supervisor.action('quick_login'))
        self.assertTrue(self.supervisor._connection_blocked)
        self.assertFalse(self.values['mobile_autostart'])

    def test_stop_during_preflight_cancels_the_original_click_even_if_stop_fails(self):
        for probe in (self.runtime,self.probe):
            with self.subTest(probe='runtime' if probe is self.runtime else 'saved'):
                self.supervisor._saved_login_attempted=False
                self.supervisor._stop.return_value=False
                def stop_during_read(*args):
                    self.assertFalse(self.supervisor.stop_napcat())
                    return dict(self.current if probe is self.runtime else self.saved)
                probe.side_effect=stop_during_read
                self.assertFalse(self.supervisor.action('quick_login'))
                self.assertTrue(self.supervisor.user_stopped)
                self.assertFalse(self.values['mobile_autostart'])
                self.submit.assert_not_called()
                value=self.current if probe is self.runtime else self.saved
                probe.side_effect=lambda *args,value=value:dict(value)

    def test_manual_stop_cancels_an_inflight_login_even_if_upstream_accepts(self):
        entered=threading.Event();release=threading.Event();answers=[]
        def submit():
            entered.set()
            self.assertTrue(release.wait(3))
            return True
        self.submit.side_effect=submit
        worker=threading.Thread(target=lambda:answers.append(self.supervisor.action('quick_login')))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertTrue(self.supervisor.stop_napcat())
        finally:
            release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(answers,[False])
        self.assertTrue(self.supervisor.user_stopped)
        self.assertFalse(self.values['mobile_autostart'])


if __name__=='__main__':unittest.main()
