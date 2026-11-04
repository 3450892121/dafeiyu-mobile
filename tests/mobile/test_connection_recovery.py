"""Transport recovery only observes synthetic QQ state and never logs in again."""
import unittest
from unittest.mock import Mock, patch

from mobile_console import MobileSupervisor


class ConnectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.supervisor=MobileSupervisor.__new__(MobileSupervisor)
        self.values={'login_uin':'190001','mobile_autostart':True}
        self.supervisor.cfg=Mock()
        self.supervisor.cfg.get.side_effect=lambda key:self.values.get(key)
        self.supervisor.cfg.set.side_effect=self.values.__setitem__
        self.supervisor._brain_process=Mock()
        self.supervisor._had_connection=True
        self.supervisor._connection_blocked=False
        self.supervisor._connection_failures=0
        self.supervisor.user_stopped=False
        self.supervisor._stop=Mock(return_value=True)
        self.supervisor._probe_qq_identity=Mock(return_value='unknown')
        self.supervisor._log_console=Mock()
        self.supervisor.napcat_running=Mock(return_value=True)
        self.supervisor.brain_running=Mock(return_value=False)
        self.supervisor.start_brain=Mock(return_value=True)
        self.supervisor.quick_login=Mock()
        self.supervisor.refresh_qr=Mock()

    def test_transport_exception_stops_brain_before_the_identity_probe(self):
        order=[]
        self.supervisor._stop.side_effect=lambda process:order.append('stopped')
        self.supervisor._probe_qq_identity.side_effect=lambda:order.append('identity')
        with patch('onebot.OneBot')as factory:
            factory.return_value.call.side_effect=ConnectionRefusedError()
            self.assertIsNone(self.supervisor.login_info())
        self.assertEqual(order,['stopped','identity'])
        self.assertFalse(self.supervisor._connection_blocked)
        self.assertTrue(self.values['mobile_autostart'])
        self.supervisor.quick_login.assert_not_called()

    def test_missing_online_status_does_not_leave_active_brain_running(self):
        with patch('onebot.OneBot')as factory:
            factory.return_value.call.return_value={}
            self.assertIsNone(self.supervisor.login_info())
            factory.return_value.get_login_info.assert_not_called()
        self.supervisor._stop.assert_called_once_with(self.supervisor._brain_process)
        self.assertFalse(self.supervisor._connection_blocked)

    def test_transport_recovery_restarts_brain_without_relogin(self):
        with patch('onebot.OneBot')as factory:
            client=factory.return_value
            client.call.side_effect=[ConnectionRefusedError(),{'online':True}]
            client.get_login_info.return_value={'user_id':190001}
            self.supervisor._poll_connection()
            self.assertEqual(self.supervisor._connection_failures,1)
            self.supervisor.start_brain.assert_not_called()
            self.supervisor._poll_connection()
        self.assertEqual(self.supervisor._connection_failures,0)
        self.assertFalse(self.supervisor._connection_blocked)
        self.supervisor.start_brain.assert_called_once_with()
        self.supervisor.quick_login.assert_not_called()
        self.supervisor.refresh_qr.assert_not_called()

    def test_dead_qq_process_stops_brain_on_first_probe_and_eventually_pauses(self):
        self.supervisor.napcat_running.return_value=False
        self.supervisor._poll_connection()
        self.supervisor._stop.assert_called_once_with(self.supervisor._brain_process)
        self.assertFalse(self.supervisor._connection_blocked)
        self.assertTrue(self.values['mobile_autostart'])
        self.supervisor._poll_connection()
        self.supervisor._poll_connection()
        self.assertTrue(self.supervisor._connection_blocked)
        self.assertFalse(self.values['mobile_autostart'])
        self.supervisor.start_brain.assert_not_called()
        self.supervisor.quick_login.assert_not_called()
        self.supervisor.refresh_qr.assert_not_called()

    def test_persisted_pause_does_not_probe_or_restart_components(self):
        self.supervisor.user_stopped=True
        self.supervisor._poll_connection()
        self.supervisor.napcat_running.assert_not_called()
        self.supervisor._stop.assert_not_called()
        self.supervisor.start_brain.assert_not_called()


if __name__=='__main__':unittest.main()
