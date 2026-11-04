"""Real project-local link fixtures for the wheel corresponding-source packager."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


PROJECT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('wheel_native_sources_tests', PROJECT / 'tools/fetch-wheel-native-sources.py')
sources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sources)


class WheelCorrespondingSourceTests(unittest.TestCase):
    def setUp(self):
        base = PROJECT / 'build/wheel-source-review-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.container = Path(self.temp.name)
        self.root = self.container / 'project'
        self.outside = self.container / 'outside-fixture'
        self.root.mkdir()
        self.outside.mkdir()
        self.cache = self.root / '.tools/wheel-native-sources'
        self.wheels = self.root / 'wheels'
        self.wheels.mkdir()
        for target in (patch.object(sources, 'ROOT', self.root),
                       patch.object(sources, 'CACHE', self.cache),
                       patch.object(sources, 'WHEELS', self.wheels),
                       patch.object(sources, 'LOCK', self.root / 'runtime/wheel-native-sources.lock.json')):
            target.start()
            self.addCleanup(target.stop)
        network = patch.object(sources.urllib.request, 'urlopen', side_effect=AssertionError('No source network in fixtures'))
        self.transport = network.start()
        self.addCleanup(network.stop)

    def directory_link(self, target, link):
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
        self.addCleanup(lambda: link.rmdir() if os.name == 'nt' else link.unlink())
        return link

    @staticmethod
    def entry():
        return {'path': 'archives/fixture.tar.gz', 'url': 'https://example.invalid/fixture.tar.gz',
                'size': 7, 'sha256': hashlib.sha256(b'fixture').hexdigest()}

    def test_a_junction_in_the_cache_ancestor_is_rejected_before_download(self):
        self.directory_link(self.outside, self.root / '.tools')
        with self.assertRaisesRegex(ValueError, 'filesystem links'):
            sources.fetch(self.entry())
        self.transport.assert_not_called()
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_existing_part_file_links_are_rejected_before_any_request(self):
        archive_dir = self.cache / 'archives'
        archive_dir.mkdir(parents=True)
        self.directory_link(self.outside, archive_dir / 'fixture.tar.gz.part')
        with self.assertRaisesRegex(ValueError, 'filesystem links'):
            sources.fetch(self.entry())
        self.transport.assert_not_called()
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_a_changed_cached_source_fails_in_check_mode_without_network(self):
        file = self.cache / 'archives/fixture.tar.gz'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'Missing or changed'):
            sources.fetch(self.entry(), check=True)
        self.transport.assert_not_called()

    def component(self, *, wrong_wheel=False, wrong_member=False):
        file = self.wheels / 'fixture-1-py3-none-any.whl'
        with zipfile.ZipFile(file, 'w') as archive:
            archive.writestr('fixture/native.so', b'fictional-native')
        return {'name': 'fixture', 'wheel': file.name,
                'wheelSha256': '0' * 64 if wrong_wheel else sources.digest(file),
                'members': [{'path': 'fixture/native.so', 'size': 16,
                             'sha256': '0' * 64 if wrong_member else hashlib.sha256(b'fictional-native').hexdigest()}]}

    def test_wrong_wheel_or_native_member_identity_fails_closed(self):
        for wrong_wheel, wrong_member in ((True, False), (False, True)):
            with self.subTest(wheel=wrong_wheel, member=wrong_member):
                with self.assertRaises(ValueError):
                    sources.check_wheel_bindings({'components': [self.component(wrong_wheel=wrong_wheel, wrong_member=wrong_member)]})

    def test_append_failure_keeps_the_original_linux_source_zip_unchanged(self):
        target = self.root / 'dist/linux-sources.zip'
        target.parent.mkdir()
        with zipfile.ZipFile(target, 'w') as archive:
            archive.writestr('linux-sources.lock.json', json.dumps({'schemaVersion': 1}))
            archive.writestr('BUILDING.txt', 'fictional source fixture')
        before = target.read_bytes()
        with self.assertRaises(ValueError):
            sources.append_zip({'components': [self.component(wrong_member=True)]}, target)
        self.assertEqual(target.read_bytes(), before)

    def test_existing_source_zip_part_link_is_rejected_before_copy(self):
        target = self.root / 'dist/linux-sources.zip'
        target.parent.mkdir()
        with zipfile.ZipFile(target, 'w') as archive:
            archive.writestr('linux-sources.lock.json', '{}')
            archive.writestr('BUILDING.txt', 'fictional source fixture')
        before = target.read_bytes()
        payload = self.root / 'payload.txt'
        payload.write_text('fictional source', encoding='utf-8')
        self.directory_link(self.outside, target.with_name(target.name + '.wheel-sources.part'))
        with patch.object(sources, 'source_payload', return_value={'wheel-native/test.txt': payload}):
            with self.assertRaisesRegex(ValueError, 'filesystem links'):
                sources.append_zip({'components': [self.component()]}, target)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
