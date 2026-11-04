"""Real archive consumers with temporary bundled fixtures and intercepted HTTP."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import mobile_asr
from mobile_control import Control
from mobile_policy import RUNTIME


spec = importlib.util.spec_from_file_location('bundled_install_qq', RUNTIME / 'install-qq.py')
install_qq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_qq)


def component(name, data, url='https://qqdl.gtimg.cn/qqfile/fixture.deb'):
    return {'file': name, 'url': url, 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest()}


class Response(io.BytesIO):
    def geturl(self):
        return 'https://qqdl.gtimg.cn/qqfile/fixture.deb'


class BundledQqInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home, self.offline = self.root / 'instance', self.root / 'offline'
        (self.home / 'downloads').mkdir(parents=True)
        (self.offline / 'archives').mkdir(parents=True)
        for target in (patch.object(install_qq, 'BASE', self.home),
                       patch.object(install_qq, 'OFFLINE', self.offline)):
            target.start()
            self.addCleanup(target.stop)

    def test_napcat_uses_only_the_verified_bundled_complete_archive(self):
        data = b'fictional-complete-archive'
        source = self.offline / 'archives/napcat.zip'
        source.write_bytes(data)
        info = component('napcat-shell.zip', data,
                         'https://github.com/example/unused.zip')
        with patch.object(install_qq.urllib.request, 'urlopen') as transport:
            self.assertEqual(install_qq.fetch(info), source)
        transport.assert_not_called()

    def test_missing_or_corrupt_napcat_never_falls_back_to_github_or_an_old_download(self):
        data = b'fictional-complete-archive'
        info = component('napcat-shell.zip', data,
                         'https://github.com/example/unused.zip')
        (self.home / 'downloads/napcat-shell.zip').write_bytes(data)
        source = self.offline / 'archives/napcat.zip'
        for invalid in (None, b'wrong-size', b'x' * len(data)):
            with self.subTest(archive=invalid is not None):
                if invalid is None:
                    source.unlink(missing_ok=True)
                else:
                    source.write_bytes(invalid)
                with patch.object(install_qq.urllib.request, 'urlopen') as transport:
                    with self.assertRaisesRegex(ValueError, '内置 NapCat'):
                        install_qq.fetch(info)
                transport.assert_not_called()

    def test_existing_verified_qq_download_is_reused_without_network_or_relogin(self):
        data = b'fictional-official-package'
        target = self.home / 'downloads/qq-arm64.deb'
        target.write_bytes(data)
        with patch.object(install_qq.urllib.request, 'urlopen') as transport:
            self.assertEqual(install_qq.fetch(component('qq-arm64.deb', data)), target)
        transport.assert_not_called()

    def test_qq_is_the_explicit_official_download_exception_with_final_hash_check(self):
        data = b'fictional-official-package'
        info = component('qq-arm64.deb', data)
        with patch.object(install_qq.urllib.request, 'urlopen', return_value=Response(data)) as transport:
            target = install_qq.fetch(info)
        self.assertEqual(transport.call_args.args[0].full_url, info['url'])
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse(target.with_name(target.name + '.part').exists())

    def test_nonofficial_or_non_https_qq_sources_are_rejected_before_any_request(self):
        values = ('http://qqdl.gtimg.cn/fixture.deb',
                  'https://github.com/example/fixture.deb',
                  'https://qqdl.gtimg.cn.unrelated.invalid/fixture.deb',
                  'https://user@qqdl.gtimg.cn/fixture.deb',
                  'https://qqdl.gtimg.cn:0/fixture.deb',
                  'https://qqdl.gtimg.cn/fixture.deb?other=source',
                  'https://qqdl.gtimg.cn/fixture.deb#fragment')
        with patch.object(install_qq.urllib.request, 'urlopen') as transport:
            for value in values:
                with self.subTest(url=value), self.assertRaises(ValueError):
                    install_qq.fetch(component('qq-arm64.deb', b'fixture', value))
        transport.assert_not_called()

    def test_qq_size_or_digest_mismatch_does_not_publish_a_cached_archive(self):
        expected = b'fictional-official-package'
        for data in (b'not-the-right-size', b'x' * len(expected)):
            with self.subTest(size=len(data)):
                with patch.object(install_qq.urllib.request, 'urlopen', return_value=Response(data)):
                    with self.assertRaises(ValueError):
                        install_qq.fetch(component('qq-arm64.deb', expected))
                self.assertFalse((self.home / 'downloads/qq-arm64.deb').exists())


class BundledModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.offline = self.root / 'offline'
        self.runtime = self.root / 'runtime'
        self.home = self.root / 'instance'
        (self.offline / 'models').mkdir(parents=True)
        self.runtime.mkdir()
        for target in (patch.object(mobile_asr, 'OFFLINE', self.offline),
                       patch.object(mobile_asr, 'RUNTIME', self.runtime)):
            target.start()
            self.addCleanup(target.stop)

    def bundle(self, malicious=None):
        archive = self.offline / 'models/sensevoice.tar.bz2'
        with tarfile.open(archive, 'w:bz2') as package:
            for name, data in (('fixture-model/model.int8.onnx', b'fictional-weights'),
                               ('fixture-model/tokens.txt', b'fictional-tokens')):
                item = tarfile.TarInfo(name)
                item.size = len(data)
                package.addfile(item, io.BytesIO(data))
            if malicious:
                item = tarfile.TarInfo(malicious)
                item.size = 3
                package.addfile(item, io.BytesIO(b'bad'))
        data = archive.read_bytes()
        self.lock = {'directory': 'fixture-model', 'url': 'https://github.com/example/unused-model',
                     'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        (self.runtime / 'asr-model.lock.json').write_text(json.dumps(self.lock), encoding='utf-8')
        return archive

    def test_model_extracts_from_bundled_archive_and_can_be_prepared_again_without_network(self):
        self.bundle()
        with patch.object(mobile_asr.urllib.request, 'urlopen') as transport:
            target = mobile_asr.install_model(self.home)
            self.assertEqual((target / 'model.int8.onnx').read_bytes(), b'fictional-weights')
            self.assertEqual((target / 'tokens.txt').read_bytes(), b'fictional-tokens')
            self.assertEqual(mobile_asr.install_model(self.home), target)
        transport.assert_not_called()

    def test_missing_bundle_does_not_download_or_consume_the_old_online_cache(self):
        archive = self.bundle()
        previous = self.home / 'asr/sensevoice.tar.bz2'
        previous.parent.mkdir(parents=True)
        previous.write_bytes(archive.read_bytes())
        archive.unlink()
        with patch.object(mobile_asr.urllib.request, 'urlopen') as transport:
            with self.assertRaisesRegex(ValueError, '内置 CPU 模型'):
                mobile_asr.install_model(self.home)
        transport.assert_not_called()

    def test_wrong_bundled_model_digest_stops_before_extraction(self):
        archive = self.bundle()
        data = archive.read_bytes()
        archive.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        with patch.object(mobile_asr.urllib.request, 'urlopen') as transport:
            with self.assertRaisesRegex(ValueError, '校验失败'):
                mobile_asr.install_model(self.home)
        self.assertFalse((self.home / 'asr/fixture-model').exists())
        transport.assert_not_called()

    def test_hash_valid_archive_with_traversal_still_cannot_escape_the_model_directory(self):
        self.bundle('fixture-model/../../outside')
        with patch.object(mobile_asr.urllib.request, 'urlopen') as transport:
            with self.assertRaisesRegex(ValueError, '越界'):
                mobile_asr.install_model(self.home)
        self.assertFalse((self.home / 'outside').exists())
        self.assertFalse((self.home / 'asr/fixture-model').exists())
        transport.assert_not_called()

    def test_control_ready_requires_the_model_and_tokens_and_prepare_uses_the_bundled_consumer(self):
        self.bundle()
        target = self.home / 'asr/fixture-model'
        target.mkdir(parents=True)
        (target / 'model.int8.onnx').write_bytes(b'fixture')
        control = Control.__new__(Control)
        control.home, control.cfg = self.home, Mock()
        control.cfg.models.return_value = []
        control.lock = threading.RLock()
        control.asr_job = {'running': False, 'message': ''}
        with patch('mobile_control.RUNTIME', self.runtime):
            self.assertFalse(control.asr_status()['local_ready'])
            (target / 'tokens.txt').write_bytes(b'fixture')
            self.assertTrue(control.asr_status()['local_ready'])

        class ImmediateThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        with patch('mobile_control.threading.Thread', ImmediateThread), \
                patch.object(mobile_asr, 'install_model', return_value=target) as prepare:
            for action in ('prepare', 'download'):
                self.assertTrue(control.asr_action({'action': action})['ok'])
                self.assertFalse(control.asr_job['running'])
        self.assertEqual(prepare.call_count, 2)


if __name__ == '__main__':
    unittest.main()
