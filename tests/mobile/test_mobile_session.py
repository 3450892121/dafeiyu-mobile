"""Login preservation uses synthetic metadata, never real credentials or QQ."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mobile_session import session_root,qq_environment,saved_login_status


class SessionDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)/'instance';self.home.mkdir()
        self.legacy=Path(self.temp.name)/'guest-root/.config/QQ'

    def seed(self,root):
        path=root/'global/nt_db/login.db';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'fictional-session-database');return path

    def test_unique_existing_directory_is_reused_without_touching_tickets(self):
        target=self.home/'qq-profile';database=self.seed(target)
        before=database.read_bytes();stamp=database.stat().st_mtime_ns
        self.assertEqual(session_root(self.home,self.legacy),target)
        self.assertEqual(database.read_bytes(),before);self.assertEqual(database.stat().st_mtime_ns,stamp)
        self.assertEqual(session_root(self.home,self.legacy),target)

    def test_guest_legacy_cache_is_pinned_in_place(self):
        database=self.seed(self.legacy)
        self.assertEqual(session_root(self.home,self.legacy),self.legacy)
        self.assertTrue(database.is_file())
        self.assertFalse((self.home/'.config/QQ').exists())

    def test_multiple_old_caches_are_not_merged_or_silently_replaced(self):
        first=self.seed(self.home/'qq-profile');second=self.seed(self.home/'.config/QQ')
        with self.assertRaises(ValueError):session_root(self.home,self.legacy)
        self.assertTrue(first.is_file());self.assertTrue(second.is_file())
        self.assertFalse((self.home/'data/qq-session-directory.json').exists())

    def test_pin_remains_stable_if_another_directory_appears(self):
        target=session_root(self.home,self.legacy)
        self.seed(self.home/'qq-profile')
        self.assertEqual(session_root(self.home,self.legacy),target)

    def test_malformed_pin_does_not_create_a_new_directory(self):
        marker=self.home/'data/qq-session-directory.json';marker.parent.mkdir()
        marker.write_text('{"directory":"../../another-app"}')
        with self.assertRaises(ValueError):session_root(self.home,self.legacy)
        self.assertEqual(marker.read_text(),'{"directory":"../../another-app"}')

    def test_payload_update_does_not_change_kernel_or_work_directory(self):
        target=self.home/'.config/QQ';database=self.seed(target)
        workdir=self.home/'Napcat/opt/QQ/resources/app/app_launcher/napcat'
        first=qq_environment(self.home,workdir,{'PATH':'fictional-toolchain'},self.legacy)
        second=qq_environment(self.home,workdir,{'PATH':'fictional-toolchain'},self.legacy)
        self.assertEqual(first,second)
        self.assertEqual(first['NAPCAT_QQ_DATA_DIR'],str(target))
        self.assertEqual(first['NAPCAT_WORKDIR'],str(workdir))
        self.assertEqual(first['HOME'],str(self.home));self.assertTrue(database.is_file())


class SavedLoginProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        config=self.home/'Napcat/opt/QQ/resources/app/app_launcher/napcat/config/webui.json'
        config.parent.mkdir(parents=True);config.write_text('{"token":"fictional-webui-token"}')

    def test_backend_probe_returns_only_target_match_not_accounts_or_credentials(self):
        responses=[io.BytesIO(b'{"data":{"Credential":"fictional-credential"}}'),io.BytesIO(b'{"code":0,"data":["190003","190001"]}')]
        with patch('mobile_session.urllib.request.urlopen',side_effect=responses)as call:
            result=saved_login_status(self.home,'190001',33112)
        self.assertEqual(result,{'available':True,'saved_account':True})
        self.assertTrue(call.call_args_list[1].args[0].full_url.endswith('/QQLogin/GetQuickLoginList'))
        self.assertFalse(any('SetQuickLogin' in c.args[0].full_url for c in call.call_args_list))
        self.assertNotIn('fictional-credential',json.dumps(result));self.assertNotIn('190003',json.dumps(result))

    def test_unavailable_probe_is_not_misreported_as_erased_cache(self):
        with patch('mobile_session.urllib.request.urlopen',side_effect=OSError('offline')):
            self.assertEqual(saved_login_status(self.home,'190001',33112),{'available':False,'saved_account':False})

    def test_another_saved_account_is_not_accepted(self):
        responses=[io.BytesIO(b'{"data":{"Credential":"fictional-credential"}}'),io.BytesIO(b'{"code":0,"data":["190003"]}')]
        with patch('mobile_session.urllib.request.urlopen',side_effect=responses):
            self.assertEqual(saved_login_status(self.home,'190001',33112),{'available':True,'saved_account':False})


if __name__=='__main__':unittest.main()
