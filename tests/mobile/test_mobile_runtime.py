"""Mobile integration contracts, synthetic configuration and temporary files only."""
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

from mobile_policy import RUNTIME, LAYOUT, config_for_mobile
from mobile_control import Control,server
import mobile_console
import mobile_backup as backup
from config import Config
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        for base in ('data','appassets','bili','backups'):(self.home/base).mkdir()
        self.cfg=Config(str(self.home/'data/config.json'))
        self.control=Control.__new__(Control);self.control.home=self.home;self.control.cfg=self.cfg
        self.control.panel=None;self.control.lock=threading.RLock();self.control.token='fictional-local-control-token-32-characters'
        self.control.asr_job={'running':False,'message':''}

    def seed_backup(self):
        (self.home/'data/.secret_key').write_bytes(b'x'*32)
        (self.home/'data/app-control.json').write_text('{"token":"current-native-token"}')
        (self.home/'data/runtime-ready.json').write_text('{"ready":true}')
        (self.home/'appassets/persona_self.txt').write_text('新的鲸鱼',encoding='utf-8')
        con=sqlite3.connect(self.home/'data/bot.db');con.execute('CREATE TABLE evidence(text)');con.execute('INSERT INTO evidence VALUES(?)',('原始资料',));con.commit();con.close()

    def test_fresh_instance_has_no_old_contacts_or_ai(self):
        self.assertEqual(self.cfg.get('owner_qq'),0)
        for key in ('allow_users','allow_groups','models'):self.assertEqual(self.cfg.get(key),[])
        self.assertEqual(self.cfg.get('login_uin'),'')
        self.assertFalse(self.cfg.get('voice_enabled'));self.assertFalse(self.cfg.get('mobile_autostart'))
        self.assertEqual(self.cfg.get('video_browse_bili_owner_uid'),0)
        self.assertEqual(self.cfg.get('video_browse_bili_self_mid'),0)
        for key in ('video_browse_like','video_browse_fav','qzone_enabled'):self.assertFalse(self.cfg.get(key))
        self.assertEqual(self.cfg.get('weather_home_city'),'')

    def test_all_fixed_ports_are_loopback_and_cannot_be_repointed(self):
        self.assertEqual(self.cfg.get('webhook_host'),'127.0.0.1')
        self.assertEqual(self.cfg.get('onebot_api'),'http://127.0.0.1:33111')
        for key,value in [('onebot_api','https://example.invalid'),('admin_port',80),('webhook_host','0.0.0.0')]:
            with self.assertRaises(ValueError):self.cfg.set(key,value)

    def test_basic_validation_does_not_partially_save(self):
        before=(self.home/'data/config.json').read_bytes()
        with self.assertRaises(ValueError):self.control.save_basic({'owner_qq':'12345','login_uin':'invalid'})
        self.assertEqual(before,(self.home/'data/config.json').read_bytes())
        with self.assertRaises(ValueError):self.control.save_basic({'login_uin':'12345','owner_qq':'12345'})
        with self.assertRaises(ValueError):self.control.save_basic({'admin_token':'new-value'})

    def test_basic_settings_persist_a_new_distinct_identity(self):
        self.control.save_basic({'login_uin':'190001','owner_qq':'190002','bot_name':'独立鲸'})
        fresh=Config(str(self.home/'data/config.json'))
        self.assertEqual(fresh.get('login_uin'),'190001');self.assertEqual(fresh.get('owner_qq'),190002)
        self.assertEqual(fresh.get('bot_name'),'独立鲸')

    def test_qq_configuration_has_authenticated_local_api_and_event_push(self):
        self.cfg.set('login_uin','190001');self.cfg.set('owner_qq',190002)
        destination=self.home/'qq-core'
        with patch.object(mobile_console,'NAPCAT',destination):
            mobile_console.qq_configs(self.cfg)
            onebot=json.loads((destination/'config/onebot11_190001.json').read_text())
            http=onebot['network']['httpServers'][0];push=onebot['network']['httpClients'][0]
            self.assertEqual((http['host'],http['port']),('127.0.0.1',33111))
            self.assertEqual(http['token'],self.cfg.get('onebot_token'))
            self.assertEqual(push['url'],'http://127.0.0.1:33113/')
            token=json.loads((destination/'config/webui.json').read_text())['token']
            mobile_console.qq_configs(self.cfg)
            self.assertEqual(token,json.loads((destination/'config/webui.json').read_text())['token'])

    def test_new_account_must_be_configured_before_qq_start(self):
        with self.assertRaises(ValueError):mobile_console.qq_configs(self.cfg)

    def test_seen_wrong_login_never_replaces_selected_account(self):
        self.cfg.set('login_uin','190001')
        supervisor=mobile_console.MobileSupervisor.__new__(mobile_console.MobileSupervisor)
        supervisor.cfg=self.cfg;supervisor._brain_process=None;supervisor._log_console=Mock()
        supervisor._remember_uin('190003')
        self.assertTrue(supervisor._connection_blocked)
        self.assertEqual(self.cfg.get('login_uin'),'190001')
        self.assertFalse(self.cfg.get('mobile_autostart'))

    def test_connection_loss_blocks_brain_and_persists_pause(self):
        supervisor=mobile_console.MobileSupervisor.__new__(mobile_console.MobileSupervisor)
        supervisor.cfg=self.cfg;supervisor._brain_process=Mock();supervisor._stop=Mock();supervisor._log_console=Mock()
        supervisor.block_connection('连接挑战')
        supervisor._stop.assert_called_once_with(supervisor._brain_process)
        self.assertFalse(Config(str(self.home/'data/config.json')).get('mobile_autostart'))

    def test_login_requires_live_online_status_and_matching_account(self):
        self.cfg.set('login_uin','190001')
        supervisor=mobile_console.MobileSupervisor.__new__(mobile_console.MobileSupervisor)
        supervisor.cfg=self.cfg;supervisor._had_connection=False;supervisor._connection_blocked=False
        supervisor._brain_process=None;supervisor._log_console=Mock()
        with patch('onebot.OneBot')as factory:
            client=factory.return_value;client.call.return_value={'online':False}
            client.get_login_info.return_value={'user_id':190001}
            self.assertIsNone(supervisor.login_info());client.get_login_info.assert_not_called()
            client.call.return_value={'online':True}
            self.assertEqual(supervisor.login_info()['user_id'],190001)
            client.get_login_info.return_value={'user_id':190003}
            self.assertIsNone(supervisor.login_info());self.assertTrue(supervisor._connection_blocked)

    def test_transport_offline_stops_active_brain_before_recovery_probes(self):
        supervisor=mobile_console.MobileSupervisor.__new__(mobile_console.MobileSupervisor)
        supervisor.cfg=self.cfg;supervisor._had_connection=True;supervisor._connection_blocked=False
        supervisor._brain_process=None;supervisor._log_console=Mock()
        supervisor._stop=Mock()
        with patch('onebot.OneBot')as factory:
            factory.return_value.call.return_value={'online':False}
            self.assertIsNone(supervisor.login_info())
        supervisor._stop.assert_called_once_with(None)
        self.assertFalse(supervisor._connection_blocked)

    def test_three_failed_transport_probes_persist_pause_without_relogin(self):
        supervisor=mobile_console.MobileSupervisor.__new__(mobile_console.MobileSupervisor)
        supervisor.cfg=self.cfg;supervisor._had_connection=True;supervisor._connection_blocked=False
        supervisor._brain_process=None;supervisor._log_console=Mock();supervisor.user_stopped=False
        supervisor._connection_failures=0;supervisor.napcat_running=Mock(return_value=True);supervisor.login_info=Mock(return_value=None)
        supervisor.quick_login=Mock();supervisor.refresh_qr=Mock()
        for _ in range(3):supervisor._poll_connection()
        self.assertTrue(supervisor._connection_blocked);self.assertFalse(self.cfg.get('mobile_autostart'))
        supervisor.quick_login.assert_not_called();supervisor.refresh_qr.assert_not_called()

    def test_stop_does_not_discover_or_create_a_foreign_panel(self):
        self.control.ensure_panel=Mock(side_effect=AssertionError('must not create panel'))
        self.control.stop();self.control.ensure_panel.assert_not_called()

    def test_status_reads_actual_components_not_presence_of_a_dictionary(self):
        self.control.panel_request=Mock(return_value={'bot':{'uptime':20},'components':{'login':False,'brain':False}})
        status=self.control.status();self.assertFalse(status['qq_online']);self.assertFalse(status['brain_running'])
        self.control.panel_request.return_value={'components':{'login':True,'brain':True,'connection_blocked':False}}
        self.assertTrue(self.control.status()['qq_online']);self.assertTrue(self.control.status()['brain_running'])

    def test_restore_preserves_native_access_and_replaces_old_database_without_stale_files(self):
        self.seed_backup();file=self.home/'backups/test.dafm';password='fictional-backup-password'
        backup.create(self.home,file,password)
        self.assertNotIn('原始资料'.encode(),file.read_bytes())
        (self.home/'data/stale.json').write_text('old');(self.home/'data/bot.db-wal').write_text('stale')
        (self.home/'appassets/persona_self.txt').write_text('changed')
        backup.restore(self.home,file,password)
        self.assertFalse((self.home/'data/stale.json').exists());self.assertFalse((self.home/'data/bot.db-wal').exists())
        self.assertEqual(json.loads((self.home/'data/app-control.json').read_text())['token'],'current-native-token')
        self.assertEqual((self.home/'appassets/persona_self.txt').read_text(encoding='utf-8'),'新的鲸鱼')
        con=sqlite3.connect(self.home/'data/bot.db');self.assertEqual(con.execute('PRAGMA quick_check').fetchone()[0],'ok');con.close()

    def test_wrong_password_and_corrupted_backup_leave_data_unchanged(self):
        self.seed_backup();file=self.home/'backups/test.dafm';backup.create(self.home,file,'fictional-password')
        original=(self.home/'data/config.json').read_bytes()
        with self.assertRaises(ValueError):backup.restore(self.home,file,'wrong-password')
        self.assertEqual(original,(self.home/'data/config.json').read_bytes())
        raw=file.read_bytes();file.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        with self.assertRaises(ValueError):backup.restore(self.home,file,'fictional-password')

    def malicious_backup(self,names):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w')as archive:
            for name in names:archive.writestr(name,b'bad')
        salt=b'a'*16;nonce=b'b'*12;password='fictional-password'
        data=backup.MAGIC+salt+nonce+AESGCM(backup.key_for(password,salt)).encrypt(nonce,stream.getvalue(),backup.MAGIC)
        file=self.home/'backups/malicious.dafm';file.write_bytes(data)
        return file,password

    def test_restore_rejects_traversal_reserved_state_and_foreign_paths(self):
        for name in ('data/../../outside','/data/file','other/config.json','data/app-control.json','data/bot.db-wal','C:/data/file','data\\..\\bad'):
            with self.subTest(name=name):
                file,password=self.malicious_backup([name])
                with self.assertRaises(ValueError):backup.restore(self.home,file,password)

    def test_restore_rolls_back_if_a_directory_commit_fails(self):
        self.seed_backup();file=self.home/'backups/test.dafm';backup.create(self.home,file,'fictional-password')
        (self.home/'appassets/persona_self.txt').write_text('keep-me')
        replace=os.replace
        def fail(source,destination):
            if Path(destination)==self.home/'appassets' and Path(source).parent!=self.home and Path(source).parent.name!='rollback':raise OSError('injected commit failure')
            return replace(source,destination)
        with patch.object(backup.os,'replace',side_effect=fail):
            with self.assertRaises(OSError):backup.restore(self.home,file,'fictional-password')
        self.assertEqual((self.home/'appassets/persona_self.txt').read_text(),'keep-me')
        self.assertTrue((self.home/'data/config.json').is_file())

    def test_asr_settings_reject_cross_host_key_use(self):
        self.cfg.models=lambda:[{'id':'speech','base_url':'https://speech.example/v1','api_key':'fictional'}]
        with self.assertRaises(ValueError):self.control.asr_action({'action':'settings','provider':'api','model_id':'speech','base_url':'https://different.example/v1'})
        self.assertTrue(self.control.asr_action({'action':'settings','provider':'api','model_id':'speech'})['ok'])

    def test_control_actions_are_explicitly_bounded(self):
        self.control.panel_request=Mock(return_value={'ok':True})
        for command in ('shell','delete_everything','local_download'):
            with self.assertRaises(ValueError):self.control.action(command)
        self.control.panel_request.assert_not_called()

    def test_omitted_desktop_backend_is_never_contacted_even_for_status(self):
        with patch('urllib.request.urlopen',side_effect=AssertionError('No desktop backend probes'))as connection:
            self.assertFalse(mobile_console.panel._cover_online(self.cfg))
            self.assertFalse(mobile_console.panel._cover_health()['online'])
        connection.assert_not_called()


class ControlHttpTests(unittest.TestCase):
    def setUp(self):
        self.control=Mock();self.control.token='fictional-auth-token-with-enough-characters'
        self.control.status.return_value={'ok':True,'qq_online':False}
        self.control.action.return_value={'ok':True}
        self.http=server(self.control,port=0)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.http.shutdown();self.http.server_close();self.thread.join(2)
    def request(self,method='GET',route='/api/status',body=None,token=True,headers=None):
        con=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=5)
        h={'Authorization':'Bearer '+self.control.token}if token else{}
        h.update(headers or{})
        con.request(method,route,body=json.dumps(body).encode()if body is not None else None,headers=h)
        response=con.getresponse();value=response.read();status=response.status;con.close();return status,value
    def test_requires_token(self):
        self.assertEqual(self.request(token=False)[0],403)
        self.assertEqual(self.request()[0],200)
    def test_rejects_cross_site_origin_and_rebinding_host(self):
        self.assertEqual(self.request(headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request(headers={'Origin':'https://evil.example'})[0],403)
        self.control.status.assert_not_called()
    def test_accepts_only_bounded_json_objects(self):
        self.assertEqual(self.request('POST','/api/action',body=[])[0],400)
        self.assertEqual(self.request('POST','/api/action',body={'command':'start_all'})[0],200)
        self.control.action.assert_called_once_with('start_all')

if __name__=='__main__':unittest.main()
