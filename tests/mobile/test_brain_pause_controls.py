"""Business pause remains visible and distinct from connection safety, using synthetic state."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from mobile_control import Control
from mobile_diagnostics import pipeline_status
from mobile_console import MobileSupervisor


class BrainPauseControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.values = {'bot_paused': True, 'mobile_autostart': False,
                       'owner_qq': 190002, 'login_uin': '190001',
                       'allow_users': [], 'allow_groups': []}
        self.cfg = Mock()
        self.cfg.get.side_effect = self.values.get
        self.cfg.set.side_effect = self.values.__setitem__
        self.cfg.models.return_value = []
        self.control = Control.__new__(Control)
        self.control.home = self.home
        self.control.cfg = self.cfg
        self.control.lock = threading.RLock()
        self.control.panel_request = Mock(return_value={
            'components': {'login': True, 'brain': True, 'connection_blocked': False}})

    def supervisor(self):
        supervisor = MobileSupervisor.__new__(MobileSupervisor)
        supervisor.cfg = self.cfg
        supervisor._connection_blocked = True
        supervisor._connection_failures = 3
        supervisor.user_stopped = True
        supervisor._log_console = Mock()
        for action in ('start_napcat', 'start_brain', 'quick_login', 'refresh_qr'):
            setattr(supervisor, action, Mock())
        return supervisor

    def test_online_process_does_not_hide_business_pause_or_change_it(self):
        result = self.control.status()
        self.assertTrue(result['qq_online'])
        self.assertTrue(result['brain_running'])
        self.assertTrue(result['brain_paused'])
        self.cfg.set.assert_not_called()
        self.values['bot_paused'] = False
        self.assertFalse(self.control.status()['brain_paused'])

    def test_pause_is_visible_while_panel_and_database_are_unavailable(self):
        self.control.panel_request.side_effect = OSError('synthetic unavailable panel')
        self.assertTrue(self.control.status()['brain_paused'])
        result = pipeline_status(self.home, self.cfg)
        self.assertTrue(result['brain_paused'])
        self.assertFalse(result['database_available'])
        self.assertEqual(list(self.home.iterdir()), [])
        self.cfg.set.assert_not_called()

    def test_explicit_resume_only_changes_business_switch_and_preserves_safety_pause(self):
        supervisor = self.supervisor()
        self.control.panel_request.side_effect = lambda route, fields: {
            'ok': supervisor.action(fields['cmd'])}
        self.assertTrue(self.control.action('resume_bot')['ok'])
        self.assertFalse(self.values['bot_paused'])
        self.assertFalse(self.values['mobile_autostart'])
        self.assertTrue(supervisor._connection_blocked)
        self.assertTrue(supervisor.user_stopped)
        self.assertEqual(supervisor._connection_failures, 3)
        self.cfg.set.assert_called_once_with('bot_paused', False)
        for action in ('start_napcat', 'start_brain', 'quick_login', 'refresh_qr'):
            getattr(supervisor, action).assert_not_called()

    def test_normal_start_does_not_undo_user_business_pause(self):
        supervisor = self.supervisor()
        supervisor.start_napcat.return_value = True
        supervisor.login_info = Mock(return_value=None)
        self.assertTrue(supervisor.action('start_all'))
        self.assertTrue(self.values['bot_paused'])
        supervisor.start_brain.assert_not_called()
        self.assertNotIn(('bot_paused', False), [call.args for call in self.cfg.set.call_args_list])


if __name__ == '__main__':
    unittest.main()
