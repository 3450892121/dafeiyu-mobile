"""Cold panel startup may wait for listening but never replay an uncertain action."""
import io
import unittest
import urllib.error
from unittest.mock import Mock, patch

from mobile_control import Control


class PanelStartupTests(unittest.TestCase):
    def setUp(self):
        self.control=Control.__new__(Control)
        self.control.cfg=Mock()
        self.control.cfg.get.return_value='fictional-admin-token'
        self.control.ensure_panel=Mock()
        self.control.panel=Mock()
        self.control.panel.poll.return_value=None

    def refused(self):return urllib.error.URLError(ConnectionRefusedError(111,'fictional refusal'))

    def test_cold_start_waits_until_listening_and_delivers_one_action(self):
        # A refusal happens before any HTTP request is accepted by the panel.
        replies=[self.refused(),self.refused(),io.BytesIO(b'{"ok":true}')]
        with patch('mobile_control.urllib.request.urlopen',side_effect=replies)as send, \
             patch('mobile_control.time.sleep')as wait:
            self.assertEqual(self.control.panel_request('/api/action',{'cmd':'start_all'}),{'ok':True})
        self.assertEqual(send.call_count,3)
        self.assertEqual(wait.call_count,2)
        self.assertEqual(send.call_args.args[0].data,b'cmd=start_all')
        self.control.ensure_panel.assert_called_once_with()

    def test_refused_connection_has_a_fixed_deadline(self):
        with patch('mobile_control.urllib.request.urlopen',side_effect=self.refused())as send, \
             patch('mobile_control.time.monotonic',side_effect=[100,115]), \
             patch('mobile_control.time.sleep')as wait:
            with self.assertRaisesRegex(OSError,'尚未就绪'):
                self.control.panel_request('/api/action',{'cmd':'quick_login'})
        send.assert_called_once();wait.assert_not_called()

    def test_dead_panel_is_not_restarted_or_replayed_inside_an_action(self):
        self.control.panel.poll.return_value=1
        with patch('mobile_control.urllib.request.urlopen',side_effect=self.refused())as send:
            with self.assertRaisesRegex(OSError,'进程已退出'):
                self.control.panel_request('/api/action',{'cmd':'quick_login'})
        send.assert_called_once();self.control.ensure_panel.assert_called_once()

    def test_timeout_reset_and_http_error_do_not_replay_login(self):
        failures=[urllib.error.URLError(TimeoutError()),
                  urllib.error.URLError(ConnectionResetError()),
                  urllib.error.HTTPError('http://127.0.0.1:33114/api/action',500,'synthetic',{},None)]
        for failure in failures:
            with self.subTest(error=type(failure).__name__), \
                 patch('mobile_control.urllib.request.urlopen',side_effect=failure)as send, \
                 patch('mobile_control.time.sleep')as wait:
                with self.assertRaises(urllib.error.URLError):
                    self.control.panel_request('/api/action',{'cmd':'quick_login'})
                send.assert_called_once();wait.assert_not_called()


if __name__=='__main__':unittest.main()
